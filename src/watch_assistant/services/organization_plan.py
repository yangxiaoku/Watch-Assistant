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

from sqlalchemy import select
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
class OrganizationPlanItem:
    source: PlanSource
    naming_plan: NamingPlan
    decision: MatchDecision

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

    def __repr__(self) -> str:
        return (
            "OrganizationPlanView(plan_id=<redacted>, plan_hash=<redacted>, "
            f"status={self.status.value!r}, revision={self.revision}, "
            f"source_count={self.source_count}, action_count={self.action_count}, "
            f"precondition_count={self.precondition_count})"
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
        }


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
            canonical = {
                "library_id": library.id,
                "source_snapshot": source_snapshot,
                "target_root": target_root,
                "actions": actions,
                "preconditions": preconditions,
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
                preconditions_json=_json(preconditions),
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
            if plan.status == OrganizationPlanStatus.IGNORED.value:
                return _view(plan)
            stale = current_time >= _utc(plan.expires_at)
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
                    current_snapshot, _, _, _, _ = _build_payload(
                        items,
                        rows,
                        target_root=plan.target_root,
                        target_conflicts=target_conflicts,
                        source_snapshot_revision=run.snapshot_revision,
                        parser_version=plan.parser_version,
                        matcher_version=plan.matcher_version,
                    )
                    if current_snapshot != json.loads(plan.source_snapshot_json):
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

    async def ignore_plan(self, plan_id: str) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            if plan.status != OrganizationPlanStatus.IGNORED.value:
                plan.status = OrganizationPlanStatus.IGNORED.value
                plan.revision += 1
                await session.commit()
            return _view(plan)

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
                "remote_version": source.remote_version,
                "source_snapshot_revision": source_snapshot_revision,
                "target": target,
                "rule_version": item.naming_plan.rule_version,
                "parser_version": parser_version,
                "matcher_version": matcher_version,
            }
        )
        actions.append(
            {
                "order": index,
                "kind": "move" if target is not None else "review",
                "object_type": source.object_type,
                "object_id": source.object_id,
                "target": target,
            }
        )
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
    actions = json.loads(plan.actions_json)
    return {
        _normalize_target(action["target"])
        for action in actions
        if isinstance(action.get("target"), str)
    }


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
    return OrganizationPlanView(
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        status=OrganizationPlanStatus(plan.status),
        revision=plan.revision,
        expires_at=_utc(plan.expires_at),
        source_count=len(json.loads(plan.source_snapshot_json)),
        action_count=len(json.loads(plan.actions_json)),
        precondition_count=len(json.loads(plan.preconditions_json)),
    )


__all__ = [
    "OrganizationPlanError",
    "OrganizationPlanItem",
    "OrganizationPlanService",
    "OrganizationPlanStatus",
    "OrganizationPlanView",
    "PlanSource",
]
