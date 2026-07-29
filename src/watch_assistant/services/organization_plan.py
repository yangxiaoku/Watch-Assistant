"""Offline, read-only organization plan previews."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.services.library_index import ScanRunState
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingPlan,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchStatus,
)


class OrganizationPlanStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    PLANNED = "planned"
    INVALIDATED = "invalidated"
    IGNORED = "ignored"


class OrganizationPlanError(ValueError):
    """Stable local error without remote values or exception details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PlanSource:
    """A caller-verified stable source observation for one planned object."""

    object_type: str
    object_id: str
    parent_id: str
    path: str
    remote_version: str
    is_directory: bool = False

    def __repr__(self) -> str:
        return "PlanSource(object_type=<redacted>, object_id=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanCompanion:
    """Optional companion source and its independently verified target."""

    source: PlanSource
    target_parent_id: str | None = None
    target_name: str | None = None

    def __repr__(self) -> str:
        return "OrganizationPlanCompanion(source=<redacted>, target=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanItem:
    source: PlanSource
    naming_plan: NamingPlan
    decision: MatchDecision
    target_parent_id: str | None = None
    target_name: str | None = None
    companions: tuple[OrganizationPlanCompanion, ...] = ()

    def __repr__(self) -> str:
        return "OrganizationPlanItem(source=<redacted>, naming_plan=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanView:
    plan_id: str
    plan_hash: str
    status: OrganizationPlanStatus
    revision: int
    expires_at: datetime
    source_count: int
    action_count: int
    precondition_count: int
    alias: str | None = None

    def __repr__(self) -> str:
        return (
            "OrganizationPlanView(plan_id=<redacted>, plan_hash=<redacted>, "
            f"status={self.status.value!r}, revision={self.revision}, "
            f"source_count={self.source_count}, action_count={self.action_count}, "
            f"precondition_count={self.precondition_count}, alias=<redacted>)"
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "revision": self.revision,
            "expires_at": self.expires_at.isoformat(),
            "source_count": self.source_count,
            "action_count": self.action_count,
            "precondition_count": self.precondition_count,
            "alias": self.alias,
        }


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanExecutionMember:
    object_type: str
    object_id: str
    source_parent_id: str
    source_path: str
    source_name: str
    source_version: str
    target_parent_id: str
    target_name: str

    def __repr__(self) -> str:
        return "OrganizationPlanExecutionMember(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanExecutionStep:
    order: int
    kind: str
    scope_directory_ids: tuple[str, ...]
    members: tuple[OrganizationPlanExecutionMember, ...]

    def __repr__(self) -> str:
        return (
            "OrganizationPlanExecutionStep(order="
            f"{self.order}, kind={self.kind!r}, member_count={len(self.members)})"
        )


class OrganizationPlanService:
    """Create and invalidate local previews without a write-capable seam."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_plan(
        self,
        *,
        library_id: str,
        scan_run_id: str,
        items: Sequence[OrganizationPlanItem],
        target_root: str = "",
        parser_version: str = "i04-v1",
        matcher_version: str = "i05-v1",
        expires_at: datetime | None = None,
        now: datetime | None = None,
        target_conflicts: Iterable[str] = (),
    ) -> OrganizationPlanView:
        _validate_identity(library_id, "invalid_library")
        _validate_identity(scan_run_id, "invalid_scan_run")
        target_root = _validate_relative_path(target_root, allow_empty=True)
        parser_version = _validate_version(parser_version)
        matcher_version = _validate_version(matcher_version)
        normalized_items = _validate_items(items)
        current_time = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is None:
            expiry = current_time + timedelta(hours=24)
        if expiry <= current_time:
            raise OrganizationPlanError("invalid_expiry")

        async with self._session_factory() as session:
            library, run = await self._verified_scan(
                session, library_id=library_id, scan_run_id=scan_run_id
            )
            rows = await self._load_source_rows(
                session, scan_run_id=run.id, items=normalized_items
            )
            source_snapshot, actions, preconditions, basis, status = _build_payload(
                normalized_items,
                rows,
                root_directory_id=library.root_directory_id,
                target_root=target_root,
                target_conflicts=target_conflicts,
                source_snapshot_revision=run.snapshot_revision,
                parser_version=parser_version,
                matcher_version=matcher_version,
            )
            rule_versions = sorted(
                {item.naming_plan.rule_version for item in normalized_items}
            )
            if len(rule_versions) != 1:
                raise OrganizationPlanError("rule_version_conflict")
            rule_version = _validate_version(rule_versions[0])
            library_snapshot = _library_snapshot(library)
            preconditions_payload = {
                "library": library_snapshot,
                "items": preconditions,
            }
            # basis_json is display/audit evidence; it is intentionally excluded.
            canonical = {
                "library_id": library.id,
                "library_snapshot": library_snapshot,
                "source_snapshot": source_snapshot,
                "target_root": target_root,
                "actions": actions,
                "preconditions": preconditions_payload,
                "rule_version": rule_version,
                "parser_version": parser_version,
                "matcher_version": matcher_version,
            }
            plan_hash = _canonical_hash(canonical)
            existing = await session.scalar(
                select(OrganizationPlan).where(
                    OrganizationPlan.plan_hash == plan_hash,
                    OrganizationPlan.library_id == library.id,
                )
            )
            if existing is not None:
                return _view(existing)
            plan = OrganizationPlan(
                id=uuid.uuid4().hex,
                library_id=library.id,
                source_scan_run_id=run.id,
                source_snapshot_revision=run.snapshot_revision,
                source_snapshot_json=_json(source_snapshot),
                target_root=target_root,
                actions_json=_json(actions),
                basis_json=_json(basis),
                preconditions_json=_json(preconditions_payload),
                rule_version=rule_version,
                parser_version=parser_version,
                matcher_version=matcher_version,
                status=status.value,
                revision=1,
                expires_at=expiry,
                plan_hash=plan_hash,
            )
            session.add(plan)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(OrganizationPlan).where(
                        OrganizationPlan.plan_hash == plan_hash,
                        OrganizationPlan.library_id == library.id,
                    )
                )
                if existing is None:
                    raise OrganizationPlanError("plan_persistence_failed") from None
                return _view(existing)
            return _view(plan)

    async def refresh_plan(
        self,
        plan_id: str,
        *,
        source_items: Sequence[OrganizationPlanItem] | None = None,
        rule_version: str | None = None,
        parser_version: str | None = None,
        matcher_version: str | None = None,
        target_conflicts: Iterable[str] = (),
        now: datetime | None = None,
    ) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        if rule_version is not None:
            rule_version = _validate_version(rule_version)
        if parser_version is not None:
            parser_version = _validate_version(parser_version)
        if matcher_version is not None:
            matcher_version = _validate_version(matcher_version)
        current_time = _utc(now)
        target_conflicts = tuple(target_conflicts)
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            stored_source_snapshot = _load_source_snapshot(plan.source_snapshot_json)
            if stored_source_snapshot is None:
                plan.status = OrganizationPlanStatus.INVALIDATED.value
                plan.revision += 1
                await session.commit()
                return _view(plan)
            if plan.status == OrganizationPlanStatus.IGNORED.value:
                return _view(plan)
            stale = current_time >= _utc(plan.expires_at)
            library = await session.get(MediaLibrary, plan.library_id)
            stored_preconditions = _load_json_object(plan.preconditions_json)
            if library is None or stored_preconditions.get(
                "library"
            ) != _library_snapshot(library):
                stale = True
            run = await session.get(LibraryScanRun, plan.source_scan_run_id)
            latest = await session.scalar(
                select(LibraryScanRun)
                .where(
                    LibraryScanRun.library_id == plan.library_id,
                    LibraryScanRun.root_directory_id == run.root_directory_id
                    if run is not None
                    else LibraryScanRun.root_directory_id == "",
                    LibraryScanRun.complete.is_(True),
                    LibraryScanRun.state == ScanRunState.COMPLETED.value,
                )
                .order_by(LibraryScanRun.snapshot_revision.desc())
                .limit(1)
            )
            if (
                run is None
                or latest is None
                or latest.id != run.id
                or run.snapshot_revision != plan.source_snapshot_revision
            ):
                stale = True
            if parser_version is not None and parser_version != plan.parser_version:
                stale = True
            if matcher_version is not None and matcher_version != plan.matcher_version:
                stale = True
            if rule_version is not None and rule_version != plan.rule_version:
                stale = True
            if source_items is not None:
                items = _validate_items(source_items)
                if run is None:
                    stale = True
                else:
                    rows = await self._load_source_rows(
                        session, run.id, items, verify_snapshot=False
                    )
                    current_snapshot, _, _, current_basis, _ = _build_payload(
                        items,
                        rows,
                        root_directory_id=library.root_directory_id
                        if library is not None
                        else "",
                        target_root=plan.target_root,
                        target_conflicts=target_conflicts,
                        source_snapshot_revision=run.snapshot_revision,
                        parser_version=plan.parser_version,
                        matcher_version=plan.matcher_version,
                    )
                    if current_snapshot != stored_source_snapshot:
                        stale = True
                    # Basis is audit-only for hashing; explicit refresh invalidates changes.
                    if current_basis != json.loads(plan.basis_json):
                        stale = True
            if any(
                _normalize_target(value) in _target_set(plan)
                for value in target_conflicts
            ):
                stale = True
            if stale:
                plan.status = OrganizationPlanStatus.INVALIDATED.value
                plan.revision += 1
                await session.commit()
            return _view(plan)

    async def ignore_plan(
        self, plan_id: str, *, expected_revision: int
    ) -> OrganizationPlanView:
        return await self.ignore_plan_at_revision(
            plan_id, expected_revision=expected_revision
        )

    async def list_plans(
        self,
        *,
        status: OrganizationPlanStatus | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> tuple[list[OrganizationPlanView], int | None]:
        if cursor < 0 or limit < 1 or limit > 100:
            raise OrganizationPlanError("invalid_pagination")
        async with self._session_factory() as session:
            statement = select(OrganizationPlan).order_by(
                OrganizationPlan.created_at.asc(), OrganizationPlan.id.asc()
            )
            if status is not None:
                statement = statement.where(OrganizationPlan.status == status.value)
            rows = list(
                (await session.scalars(statement.offset(cursor).limit(limit + 1))).all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = cursor + limit if has_more else None
            return [_view(row) for row in rows], next_cursor

    async def get_plan(self, plan_id: str) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            return _view(plan)

    async def plan_library_id(self, plan_id: str) -> str:
        """Return the owning library ID for a scope check at an adapter boundary."""

        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            return plan.library_id

    async def confirm_plan(
        self, plan_id: str, *, expected_revision: int
    ) -> OrganizationPlanView:
        return await self._transition_plan(
            plan_id,
            expected_revision=expected_revision,
            target=OrganizationPlanStatus.PLANNED,
            allowed=(
                OrganizationPlanStatus.NEEDS_REVIEW,
                OrganizationPlanStatus.PLANNED,
            ),
        )

    async def ignore_plan_at_revision(
        self, plan_id: str, *, expected_revision: int
    ) -> OrganizationPlanView:
        return await self._transition_plan(
            plan_id,
            expected_revision=expected_revision,
            target=OrganizationPlanStatus.IGNORED,
            allowed=tuple(OrganizationPlanStatus),
        )

    async def alias_plan(
        self, plan_id: str, *, alias: str, expected_revision: int
    ) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        alias = _validate_alias(alias)
        if expected_revision < 0:
            raise OrganizationPlanError("invalid_revision")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            if plan.revision != expected_revision:
                raise OrganizationPlanError("stale_revision")
            if plan.status not in {
                OrganizationPlanStatus.NEEDS_REVIEW.value,
                OrganizationPlanStatus.PLANNED.value,
            }:
                raise OrganizationPlanError("plan_not_reviewable")
            if plan.alias == alias:
                return _view(plan)
            result = await session.execute(
                update(OrganizationPlan)
                .where(
                    OrganizationPlan.id == plan_id,
                    OrganizationPlan.revision == expected_revision,
                    OrganizationPlan.status.in_(
                        (
                            OrganizationPlanStatus.NEEDS_REVIEW.value,
                            OrganizationPlanStatus.PLANNED.value,
                        )
                    ),
                )
                .values(alias=alias, revision=expected_revision + 1)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise OrganizationPlanError("stale_revision")
            await session.commit()
            refreshed = await session.get(OrganizationPlan, plan_id)
            if refreshed is None:
                raise OrganizationPlanError("plan_not_found")
            return _view(refreshed)

    async def _transition_plan(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        target: OrganizationPlanStatus,
        allowed: tuple[OrganizationPlanStatus, ...],
    ) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        if expected_revision < 0:
            raise OrganizationPlanError("invalid_revision")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            if plan.revision != expected_revision:
                raise OrganizationPlanError("stale_revision")
            if plan.status == target.value:
                return _view(plan)
            if plan.status not in {item.value for item in allowed}:
                raise OrganizationPlanError("plan_not_reviewable")
            result = await session.execute(
                update(OrganizationPlan)
                .where(
                    OrganizationPlan.id == plan_id,
                    OrganizationPlan.revision == expected_revision,
                )
                .values(status=target.value, revision=expected_revision + 1)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise OrganizationPlanError("stale_revision")
            await session.commit()
            refreshed = await session.get(OrganizationPlan, plan_id)
            if refreshed is None:
                raise OrganizationPlanError("plan_not_found")
            return _view(refreshed)

    async def _verified_scan(
        self,
        session: AsyncSession,
        *,
        library_id: str,
        scan_run_id: str,
    ) -> tuple[MediaLibrary, LibraryScanRun]:
        library = await session.get(MediaLibrary, library_id)
        run = await session.get(LibraryScanRun, scan_run_id)
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or run is None
            or run.library_id != library_id
            or run.root_directory_id != library.root_directory_id
            or run.state != ScanRunState.COMPLETED.value
            or not run.complete
            or run.snapshot_revision is None
        ):
            raise OrganizationPlanError("scan_not_current")
        latest = await session.scalar(
            select(LibraryScanRun)
            .where(
                LibraryScanRun.library_id == library_id,
                LibraryScanRun.root_directory_id == library.root_directory_id,
                LibraryScanRun.state == ScanRunState.COMPLETED.value,
                LibraryScanRun.complete.is_(True),
            )
            .order_by(LibraryScanRun.snapshot_revision.desc())
            .limit(1)
        )
        if latest is None or latest.id != run.id:
            raise OrganizationPlanError("scan_not_current")
        return library, run

    async def _load_source_rows(
        self,
        session: AsyncSession,
        scan_run_id: str,
        items: Sequence[OrganizationPlanItem],
        *,
        verify_snapshot: bool = True,
    ) -> dict[tuple[str, str], LibraryScanEntry]:
        rows = {
            (row.object_type, row.object_id): row
            for row in await session.scalars(
                select(LibraryScanEntry).where(
                    LibraryScanEntry.scan_run_id == scan_run_id
                )
            )
        }
        for item in items:
            key = (item.source.object_type, item.source.object_id)
            row = rows.get(key)
            if row is None or (
                verify_snapshot
                and (
                    row.parent_id != item.source.parent_id
                    or row.path != item.source.path
                    or row.is_directory != item.source.is_directory
                )
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
        return rows


def _build_payload(
    items: Sequence[OrganizationPlanItem],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
    *,
    root_directory_id: str,
    target_root: str,
    target_conflicts: Iterable[str],
    source_snapshot_revision: int | None,
    parser_version: str,
    matcher_version: str,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    OrganizationPlanStatus,
]:
    conflicts = {
        _normalize_target(_validate_relative_path(value)) for value in target_conflicts
    }
    source_snapshot: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    preconditions: list[dict[str, object]] = []
    basis: list[dict[str, object]] = []
    targets: dict[str, int] = {}
    directory_rows: dict[str, list[LibraryScanEntry]] = {}
    for row in rows.values():
        if row.is_directory:
            directory_rows.setdefault(row.object_id, []).append(row)
    planned = True
    for index, item in enumerate(items):
        source = item.source
        key = (source.object_type, source.object_id)
        if key not in rows:
            raise OrganizationPlanError("source_snapshot_mismatch")
        target = _target_path(target_root, item.naming_plan.target_path)
        source_snapshot.append(
            {
                "object_type": source.object_type,
                "object_id": source.object_id,
                "parent_id": source.parent_id,
                "path": source.path,
                "name": rows[key].name,
                "remote_version": source.remote_version,
                "is_directory": source.is_directory,
            }
        )
        preconditions.append(
            {
                "source_index": index,
                "object_type": source.object_type,
                "object_id": source.object_id,
                "parent_id": source.parent_id,
                "source_path": source.path,
                "source_name": rows[key].name,
                "remote_version": source.remote_version,
                "source_snapshot_revision": source_snapshot_revision,
                "target": target,
                "target_parent_id": item.target_parent_id,
                "target_name": item.target_name,
                "rule_version": item.naming_plan.rule_version,
                "parser_version": parser_version,
                "matcher_version": matcher_version,
            }
        )
        execution = _execution_payload(
            item,
            companions=rows,
            directory_rows=directory_rows,
            root_directory_id=root_directory_id,
            target=target,
            order=index,
        )
        preconditions[-1]["execution"] = execution
        actions.append(
            {
                "order": index,
                "kind": "move" if target is not None else "review",
                "object_type": source.object_type,
                "object_id": source.object_id,
                "source_parent_id": source.parent_id,
                "source_path": source.path,
                "source_name": rows[key].name,
                "source_version": source.remote_version,
                "target_parent_id": item.target_parent_id,
                "target_name": item.target_name,
                "target": target,
                "execution": execution,
            }
        )
        if execution is not None:
            source_snapshot[-1]["companions"] = [
                {
                    "object_type": member["object_type"],
                    "object_id": member["object_id"],
                    "parent_id": member["source_parent_id"],
                    "path": member["source_path"],
                    "name": member["source_name"],
                    "remote_version": member["source_version"],
                    "is_directory": False,
                }
                for member in execution["members"][1:]
            ]
        if target is not None:
            targets[_normalize_target(target)] = (
                targets.get(_normalize_target(target), 0) + 1
            )
        accepted = (
            item.naming_plan.status is ClassificationStatus.PLANNED
            and item.decision.status is MatchStatus.ACCEPTED
            and item.decision.confidence is MatchConfidence.HIGH
            and item.decision.selected is not None
            and target is not None
            and execution is not None
        )
        if not accepted:
            planned = False
            actions[-1]["kind"] = "review"
        reasons = tuple(
            _safe_reason(reason.value if hasattr(reason, "value") else reason)
            for reason in item.naming_plan.reasons
        )
        basis.append(
            {
                "source_index": index,
                "classification_status": item.naming_plan.status.value,
                "match_status": item.decision.status.value,
                "match_confidence": (
                    item.decision.confidence.value
                    if item.decision.confidence is not None
                    else None
                ),
                "accepted": accepted,
                "reasons": reasons,
            }
        )
    if any(count > 1 for count in targets.values()) or conflicts & set(targets):
        planned = False
    conflict_keys = conflicts | {key for key, count in targets.items() if count > 1}
    for action, precondition in zip(actions, preconditions, strict=True):
        target = action["target"]
        is_conflict = (
            isinstance(target, str) and _normalize_target(target) in conflict_keys
        )
        action["kind"] = "review" if is_conflict else action["kind"]
        precondition["target_conflict"] = is_conflict
    status = (
        OrganizationPlanStatus.PLANNED
        if planned
        else OrganizationPlanStatus.NEEDS_REVIEW
    )
    source_snapshot.sort(key=_source_key)
    actions.sort(key=lambda value: int(value["order"]))
    preconditions.sort(key=lambda value: int(value["source_index"]))
    basis.sort(key=lambda value: int(value["source_index"]))
    return source_snapshot, actions, preconditions, basis, status


def _target_set(plan: OrganizationPlan) -> set[str]:
    actions = _load_json_list(plan.actions_json)
    return {
        _normalize_target(action["target"])
        for action in actions
        if isinstance(action, dict) and isinstance(action.get("target"), str)
    }


def _execution_payload(
    item: OrganizationPlanItem,
    *,
    companions: Mapping[tuple[str, str], LibraryScanEntry],
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
    target: str | None,
    order: int,
) -> dict[str, object] | None:
    if target is None or not _safe_identity(root_directory_id):
        return None
    target_parent_id = item.target_parent_id
    target_name = item.target_name
    if not _valid_target_name(target_name) or not _safe_identity(target_parent_id):
        return None
    if target_name != PurePosixPath(target).name:
        return None
    if not _target_directory_matches(
        target,
        target_parent_id=target_parent_id,
        directory_rows=directory_rows,
        root_directory_id=root_directory_id,
    ):
        return None
    members: list[dict[str, str]] = []
    member_keys: set[tuple[str, str]] = set()
    all_sources = (
        (item.source, target_parent_id, target_name),
        *(
            (companion.source, companion.target_parent_id, companion.target_name)
            for companion in item.companions
        ),
    )
    for source, member_target_parent, member_target_name in all_sources:
        key = (source.object_type, source.object_id)
        member_row = companions.get(key)
        if (
            key in member_keys
            or member_row is None
            or member_row.is_directory
            or not _safe_identity(source.object_type)
            or not _safe_identity(source.object_id)
            or not _safe_identity(source.parent_id)
            or not isinstance(source.remote_version, str)
            or not source.remote_version
            or not _valid_source_version(source.remote_version)
            or not _safe_identity(member_target_parent)
            or not _valid_target_name(member_target_name)
            or member_target_parent != target_parent_id
            or not _valid_source_path(source.path)
            or not _valid_target_name(member_row.name)
            or not _valid_source_path(member_row.path)
            or member_row.parent_id != source.parent_id
            or member_row.path != source.path
        ):
            return None
        if not _source_parent_is_managed(
            member_row.parent_id, directory_rows, root_directory_id
        ):
            return None
        member_keys.add(key)
        members.append(
            {
                "object_type": source.object_type,
                "object_id": source.object_id,
                "source_parent_id": member_row.parent_id,
                "source_path": member_row.path,
                "source_name": member_row.name,
                "source_version": source.remote_version,
                "target_parent_id": member_target_parent,
                "target_name": member_target_name,
            }
        )
    scope_directory_ids = sorted(
        {
            root_directory_id,
            *(member["source_parent_id"] for member in members),
            *(member["target_parent_id"] for member in members),
        }
    )
    return {
        "order": order,
        "kind": "move",
        "scope_directory_ids": scope_directory_ids,
        "members": members,
    }


def _target_directory_matches(
    target: str,
    *,
    target_parent_id: str,
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
) -> bool:
    parent_path = PurePosixPath(target).parent
    if str(parent_path) == ".":
        return target_parent_id == root_directory_id
    directories = directory_rows.get(target_parent_id, ())
    if len(directories) != 1:
        return False
    directory = directories[0]
    if not isinstance(directory.path, str):
        return False
    return _index_path(directory.path) == _index_path(str(parent_path))


def _source_parent_is_managed(
    parent_id: str,
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
) -> bool:
    return parent_id == root_directory_id or len(directory_rows.get(parent_id, ())) == 1


def _index_path(value: str) -> str:
    return value.strip("/").replace("\\", "/")


def _valid_target_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4096
        and value not in {".", ".."}
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
    )


async def load_executable_steps(
    session_factory: async_sessionmaker[AsyncSession],
    plan: OrganizationPlan | str,
) -> tuple[OrganizationPlanExecutionStep, ...] | None:
    """Load executable steps only after revalidating durable plan state."""

    plan_id = plan if isinstance(plan, str) else getattr(plan, "id", None)
    if not _safe_identity(plan_id):
        return None
    async with session_factory() as session:
        stored = await session.get(OrganizationPlan, plan_id)
        if stored is None or stored.status != OrganizationPlanStatus.PLANNED.value:
            return None
        library = await session.get(MediaLibrary, stored.library_id)
        run = await session.get(LibraryScanRun, stored.source_scan_run_id)
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or run is None
            or run.library_id != stored.library_id
            or run.root_directory_id != library.root_directory_id
            or run.state != ScanRunState.COMPLETED.value
            or not run.complete
            or run.snapshot_revision != stored.source_snapshot_revision
            or _utc(stored.expires_at) <= datetime.now(UTC)
        ):
            return None
        latest = await session.scalar(
            select(LibraryScanRun)
            .where(
                LibraryScanRun.library_id == stored.library_id,
                LibraryScanRun.root_directory_id == library.root_directory_id,
                LibraryScanRun.state == ScanRunState.COMPLETED.value,
                LibraryScanRun.complete.is_(True),
            )
            .order_by(LibraryScanRun.snapshot_revision.desc())
            .limit(1)
        )
        if latest is None or latest.id != run.id:
            return None
        source_snapshot = _load_source_snapshot(
            stored.source_snapshot_json, require_name=True
        )
        actions = _load_json_list(stored.actions_json)
        preconditions = _load_json_object(stored.preconditions_json)
        precondition_items = preconditions.get("items")
        if (
            source_snapshot is None
            or not actions
            or preconditions.get("library") != _library_snapshot(library)
            or not isinstance(precondition_items, list)
            or len(precondition_items) != len(actions)
        ):
            return None
        steps = _parse_executable_steps(stored)
        if steps is None:
            return None
        rows = {
            (row.object_type, row.object_id): row
            for row in await session.scalars(
                select(LibraryScanEntry).where(LibraryScanEntry.scan_run_id == run.id)
            )
        }
        if not _validate_persisted_execution(
            stored,
            library=library,
            run=run,
            source_snapshot=source_snapshot,
            actions=actions,
            preconditions=precondition_items,
            steps=steps,
            rows=rows,
        ):
            return None
        canonical = {
            "library_id": stored.library_id,
            "library_snapshot": _library_snapshot(library),
            "source_snapshot": source_snapshot,
            "target_root": stored.target_root,
            "actions": actions,
            "preconditions": preconditions,
            "rule_version": stored.rule_version,
            "parser_version": stored.parser_version,
            "matcher_version": stored.matcher_version,
        }
        if stored.plan_hash != _canonical_hash(canonical):
            return None
        return steps


def _parse_executable_steps(
    plan: OrganizationPlan,
) -> tuple[OrganizationPlanExecutionStep, ...] | None:
    """Parse only the complete execution payload used by a future executor.

    Old previews remain readable, but their actions intentionally return ``None``
    here because they do not carry the stable directory and member identities.
    """

    if plan.status != OrganizationPlanStatus.PLANNED.value:
        return None
    actions = _load_json_list(plan.actions_json)
    if not actions:
        return None
    steps: list[OrganizationPlanExecutionStep] = []
    seen_members: set[tuple[str, str]] = set()
    for expected_order, action in enumerate(actions):
        if not isinstance(action, dict) or action.get("kind") != "move":
            return None
        execution = action.get("execution")
        if not isinstance(execution, dict):
            return None
        order = execution.get("order")
        kind = execution.get("kind")
        scope = execution.get("scope_directory_ids")
        members = execution.get("members")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order != expected_order
            or kind != "move"
            or not isinstance(scope, list)
            or not scope
            or not all(_safe_identity(value) for value in scope)
            or len(scope) != len(set(scope))
            or scope != sorted(scope)
            or not isinstance(members, list)
            or not members
        ):
            return None
        parsed_members: list[OrganizationPlanExecutionMember] = []
        for member in members:
            if not isinstance(member, dict):
                return None
            values = (
                member.get("object_type"),
                member.get("object_id"),
                member.get("source_parent_id"),
                member.get("source_path"),
                member.get("source_name"),
                member.get("source_version"),
                member.get("target_parent_id"),
                member.get("target_name"),
            )
            (
                object_type,
                object_id,
                source_parent,
                source_path,
                source_name,
                version,
                target_parent,
                target_name,
            ) = values
            if (
                not _safe_identity(object_type)
                or not _safe_identity(object_id)
                or not _safe_identity(source_parent)
                or not _valid_source_path(source_path)
                or not _valid_target_name(source_name)
                or not _valid_source_version(version)
                or not _safe_identity(target_parent)
                or not _valid_target_name(target_name)
            ):
                return None
            key = (object_type, object_id)
            if key in seen_members:
                return None
            seen_members.add(key)
            parsed_members.append(
                OrganizationPlanExecutionMember(
                    object_type=object_type,
                    object_id=object_id,
                    source_parent_id=source_parent,
                    source_path=source_path,
                    source_name=source_name,
                    source_version=version,
                    target_parent_id=target_parent,
                    target_name=target_name,
                )
            )
        if (
            action.get("order") != expected_order
            or action.get("object_type") != parsed_members[0].object_type
            or action.get("object_id") != parsed_members[0].object_id
            or not set(scope).issuperset(
                {member.source_parent_id for member in parsed_members}
                | {member.target_parent_id for member in parsed_members}
            )
        ):
            return None
        steps.append(
            OrganizationPlanExecutionStep(
                order=order,
                kind=kind,
                scope_directory_ids=tuple(scope),
                members=tuple(parsed_members),
            )
        )
    return tuple(steps)


def _validate_persisted_execution(
    plan: OrganizationPlan,
    *,
    library: MediaLibrary,
    run: LibraryScanRun,
    source_snapshot: list[dict[str, object]],
    actions: list[object],
    preconditions: list[object],
    steps: tuple[OrganizationPlanExecutionStep, ...],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
) -> bool:
    if len(source_snapshot) != len(actions) or len(steps) != len(actions):
        return False
    source_by_key = {
        (item["object_type"], item["object_id"]): item for item in source_snapshot
    }
    if len(source_by_key) != len(source_snapshot):
        return False
    directory_rows: dict[str, list[LibraryScanEntry]] = {}
    for row in rows.values():
        if row.is_directory:
            directory_rows.setdefault(row.object_id, []).append(row)
    managed_directory_ids = {library.root_directory_id}
    managed_directory_ids.update(
        object_id for object_id, matches in directory_rows.items() if len(matches) == 1
    )
    try:
        _validate_relative_path(plan.target_root, allow_empty=True)
        _validate_version(plan.rule_version)
        _validate_version(plan.parser_version)
        _validate_version(plan.matcher_version)
    except OrganizationPlanError:
        return False
    for index, (action, precondition, step) in enumerate(
        zip(actions, preconditions, steps, strict=True)
    ):
        if not isinstance(action, dict) or not isinstance(precondition, dict):
            return False
        object_key = (action.get("object_type"), action.get("object_id"))
        snapshot = source_by_key.get(object_key)
        current_row = rows.get(object_key)
        execution = action.get("execution")
        if (
            snapshot is None
            or precondition.get("source_index") != index
            or precondition.get("object_type") != action.get("object_type")
            or precondition.get("object_id") != action.get("object_id")
            or precondition.get("parent_id") != snapshot.get("parent_id")
            or precondition.get("source_path") != snapshot.get("path")
            or precondition.get("remote_version") != snapshot.get("remote_version")
            or action.get("source_parent_id") != snapshot.get("parent_id")
            or action.get("source_path") != snapshot.get("path")
            or action.get("source_name") != snapshot.get("name")
            or action.get("source_version") != snapshot.get("remote_version")
            or precondition.get("source_snapshot_revision")
            != plan.source_snapshot_revision
            or precondition.get("target") != action.get("target")
            or precondition.get("parent_id") != action.get("source_parent_id")
            or precondition.get("source_path") != action.get("source_path")
            or precondition.get("source_name") != action.get("source_name")
            or precondition.get("remote_version") != action.get("source_version")
            or precondition.get("target_parent_id") != action.get("target_parent_id")
            or precondition.get("target_name") != action.get("target_name")
            or precondition.get("rule_version") != plan.rule_version
            or precondition.get("parser_version") != plan.parser_version
            or precondition.get("matcher_version") != plan.matcher_version
            or precondition.get("target_conflict") is not False
            or precondition.get("execution") != execution
            or not isinstance(execution, dict)
            or action.get("kind") != "move"
            or action.get("order") != index
            or step.order != index
            or current_row is None
            or snapshot.get("name") != current_row.name
            or current_row.parent_id != snapshot.get("parent_id")
            or current_row.path != snapshot.get("path")
            or current_row.is_directory is not False
        ):
            return False
        if not _validate_persisted_step(
            action,
            step=step,
            library=library,
            managed_directory_ids=managed_directory_ids,
            directory_rows=directory_rows,
            rows=rows,
            snapshot=snapshot,
        ):
            return False
    return run.snapshot_revision == plan.source_snapshot_revision


def _validate_persisted_step(
    action: dict[str, object],
    *,
    step: OrganizationPlanExecutionStep,
    library: MediaLibrary,
    managed_directory_ids: set[str],
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
    snapshot: dict[str, object],
) -> bool:
    target = action.get("target")
    if not isinstance(target, str):
        return False
    try:
        _validate_relative_path(target)
    except OrganizationPlanError:
        return False
    if not step.members:
        return False
    primary = step.members[0]
    if (
        primary.object_type != action.get("object_type")
        or primary.object_id != action.get("object_id")
        or primary.source_parent_id != action.get("source_parent_id")
        or primary.source_path != action.get("source_path")
        or primary.source_name != action.get("source_name")
        or primary.source_version != action.get("source_version")
        or primary.target_parent_id != action.get("target_parent_id")
        or primary.target_name != action.get("target_name")
        or primary.target_name != PurePosixPath(target).name
        or not _target_directory_matches(
            target,
            target_parent_id=primary.target_parent_id,
            directory_rows=directory_rows,
            root_directory_id=library.root_directory_id,
        )
    ):
        return False
    companion_snapshot = snapshot.get("companions", [])
    if not isinstance(companion_snapshot, list):
        return False
    snapshot_members = [snapshot, *companion_snapshot]
    if len(snapshot_members) != len(step.members):
        return False
    if any(
        not isinstance(item, dict)
        or item.get("object_type") != member.object_type
        or item.get("object_id") != member.object_id
        or item.get("parent_id") != member.source_parent_id
        or item.get("path") != member.source_path
        or item.get("name") != member.source_name
        or item.get("remote_version") != member.source_version
        or item.get("is_directory") is not False
        for item, member in zip(snapshot_members, step.members, strict=True)
    ):
        return False
    expected_scope = {
        library.root_directory_id,
        *(member.source_parent_id for member in step.members),
        *(member.target_parent_id for member in step.members),
    }
    if (
        set(step.scope_directory_ids) != expected_scope
        or not expected_scope <= managed_directory_ids
    ):
        return False
    for member in step.members:
        row = rows.get((member.object_type, member.object_id))
        if (
            row is None
            or row.is_directory
            or row.parent_id != member.source_parent_id
            or row.path != member.source_path
            or row.name != member.source_name
            or not _source_parent_is_managed(
                member.source_parent_id, directory_rows, library.root_directory_id
            )
            or member.target_parent_id != primary.target_parent_id
            or not _valid_target_name(member.target_name)
        ):
            return False
    return True


def _library_snapshot(library: MediaLibrary) -> dict[str, object]:
    return {
        "library_id": library.id,
        "revision": library.revision,
        "enabled": library.enabled,
        "scope_verified": library.scope_verified,
        "root_directory_id": library.root_directory_id,
    }


def _load_json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_source_snapshot(
    value: object, *, require_name: bool = False
) -> list[dict[str, object]] | None:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    for item in parsed:
        if not isinstance(item, dict):
            return None
        object_type = item.get("object_type")
        object_id = item.get("object_id")
        parent_id = item.get("parent_id")
        path = item.get("path")
        remote_version = item.get("remote_version")
        if (
            not _safe_identity(object_type)
            or not _safe_identity(object_id)
            or not _safe_identity(parent_id)
            or not _valid_source_path(path)
            or not _valid_source_version(remote_version)
            or item.get("is_directory") is not False
            or (require_name and not _valid_target_name(item.get("name")))
        ):
            return None
        companions = item.get("companions", [])
        if not isinstance(companions, list):
            return None
        seen_companions: set[tuple[object, object]] = set()
        for companion in companions:
            if not isinstance(companion, dict):
                return None
            companion_key = (
                companion.get("object_type"),
                companion.get("object_id"),
            )
            if (
                not _safe_identity(companion_key[0])
                or not _safe_identity(companion_key[1])
                or companion_key in seen_companions
                or companion_key == (object_type, object_id)
                or not _safe_identity(companion.get("parent_id"))
                or not _valid_source_path(companion.get("path"))
                or not _valid_target_name(companion.get("name"))
                or not _valid_source_version(companion.get("remote_version"))
                or companion.get("is_directory") is not False
            ):
                return None
            seen_companions.add(companion_key)
    return parsed


def _safe_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and "://" not in value
    )


def _load_json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _target_path(root: str, path: str | None) -> str | None:
    if path is None:
        return None
    normalized = _validate_relative_path(path)
    if not root:
        return normalized
    return _validate_relative_path(f"{root}/{normalized}")


def _validate_items(
    items: Sequence[OrganizationPlanItem],
) -> tuple[OrganizationPlanItem, ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise OrganizationPlanError("invalid_plan_items")
    result = tuple(items)
    keys: set[tuple[str, str]] = set()
    for item in result:
        if not isinstance(item, OrganizationPlanItem):
            raise OrganizationPlanError("invalid_plan_items")
        source = item.source
        _validate_identity(source.object_type, "invalid_source_identity")
        _validate_identity(source.object_id, "invalid_source_identity")
        _validate_identity(source.parent_id, "invalid_source_parent")
        if (
            not isinstance(source.path, str)
            or not source.path
            or "\x00" in source.path
            or len(source.path) > 4096
            or not isinstance(source.remote_version, str)
            or not source.remote_version
            or len(source.remote_version) > 128
            or not _valid_source_version(source.remote_version)
            or not isinstance(source.is_directory, bool)
        ):
            raise OrganizationPlanError("invalid_source_snapshot")
        if source.is_directory:
            raise OrganizationPlanError("source_not_file")
        key = (source.object_type, source.object_id)
        if key in keys:
            raise OrganizationPlanError("duplicate_source")
        keys.add(key)
        if not isinstance(item.naming_plan, NamingPlan) or not isinstance(
            item.decision, MatchDecision
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if not isinstance(item.companions, Sequence) or isinstance(
            item.companions, (str, bytes)
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if any(
            not isinstance(companion, OrganizationPlanCompanion)
            for companion in item.companions
        ):
            raise OrganizationPlanError("invalid_plan_input")
    return tuple(
        sorted(
            result, key=lambda item: (item.source.object_type, item.source.object_id)
        )
    )


def _validate_identity(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        raise OrganizationPlanError(code)
    return value


def _valid_source_version(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        return False
    return not any(
        marker in value.casefold()
        for marker in ("pickcode", "cookie", "token", "password", "secret")
    )


def _valid_source_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or "\\" in value
        or "://" in value
    ):
        return False
    parts = value.split("/")
    if value.startswith("/"):
        parts = parts[1:]
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _validate_alias(value: str) -> str:
    if not isinstance(value, str):
        raise OrganizationPlanError("invalid_alias")
    value = value.strip()
    lowered = value.casefold()
    if (
        not value
        or len(value) > 64
        or not any(character.isalpha() for character in value)
        or any(character.isspace() and character not in {" "} for character in value)
        or any(character in value for character in ("/", "\\", "\x00", ":"))
        or "://" in value
        or any(
            marker in lowered
            for marker in ("cookie", "pickcode", "token", "password", "secret")
        )
        or value in {".", ".."}
    ):
        raise OrganizationPlanError("invalid_alias")
    return value


def _validate_version(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or "\x00" in value
        or any(character.isspace() for character in value)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
    ):
        raise OrganizationPlanError("invalid_version")
    return value


def _validate_relative_path(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise OrganizationPlanError("invalid_target_path")
    if not value and allow_empty:
        return ""
    if (
        not value
        or value.startswith(("/", "\\", "//"))
        or re.match(r"^[A-Za-z]:", value)
        or "://" in value
        or "\\" in value
    ):
        raise OrganizationPlanError("invalid_target_path")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OrganizationPlanError("invalid_target_path")
    return "/".join(parts)


def _source_key(value: Mapping[str, object]) -> tuple[str, str]:
    return str(value["object_type"]), str(value["object_id"])


def _normalize_target(value: str) -> str:
    return value.casefold()


def _safe_reason(value: object) -> str:
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", text):
        return "reason_redacted"
    return text


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _view(plan: OrganizationPlan) -> OrganizationPlanView:
    source_snapshot = _load_source_snapshot(plan.source_snapshot_json) or []
    preconditions = _load_json_object(plan.preconditions_json)
    if isinstance(preconditions, dict):
        precondition_count = len(preconditions.get("items", ()))
    else:
        precondition_count = len(preconditions)
    return OrganizationPlanView(
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        status=OrganizationPlanStatus(plan.status),
        revision=plan.revision,
        expires_at=_utc(plan.expires_at),
        source_count=len(source_snapshot),
        action_count=len(_load_json_list(plan.actions_json)),
        precondition_count=precondition_count,
        alias=plan.alias,
    )


__all__ = [
    "OrganizationPlanCompanion",
    "OrganizationPlanError",
    "OrganizationPlanExecutionMember",
    "OrganizationPlanExecutionStep",
    "OrganizationPlanItem",
    "OrganizationPlanService",
    "OrganizationPlanStatus",
    "OrganizationPlanView",
    "PlanSource",
    "load_executable_steps",
]
