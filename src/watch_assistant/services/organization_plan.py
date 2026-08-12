"""Offline, read-only organization plan previews."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationHistoryEntry,
    OrganizationPlan,
)
from watch_assistant.models import OrganizationOperation
from watch_assistant.services.library_index import (
    LibraryIndexError,
    ScanRunState,
    validate_complete_scan_evidence,
)
from watch_assistant.services.media_classification import (
    NamingRuleConfig,
    plan_media,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchSource,
    MatchStatus,
    TmdbCandidate,
    TmdbMatchClient,
    TmdbMatcher,
    build_match_input,
)
from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.observability import audit_event
from watch_assistant.services.organization_plan_helpers import (
    _basis_for_source,
    _build_payload,
    _canonical_hash,
    _directory_id_for_path,
    _entry_remote_version,
    _json,
    _latest_completed_scan,
    _library_snapshot,
    _load_json_list,
    _load_json_object,
    _load_source_snapshot,
    _merge_candidate_evidence,
    _normalize_target,
    _plan_scan_binding_is_current,
    _reconcile_target_conflicts,
    _resolve_source_index,
    _scan_is_valid_for_library,
    _source_observation_matches,
    _source_snapshot_changed,
    _source_version_matches,
    _target_set,
    _utc,
    _validate_alias,
    _validate_candidate_query,
    _validate_identity,
    _validate_items,
    _validate_relative_path,
    _validate_target_directories,
    _validate_version,
    _view,
    load_executable_steps,
)
from watch_assistant.services.organization_plan_models import (
    OrganizationExecutionBlocker,
    OrganizationPlanCompanion,
    OrganizationPlanError,
    OrganizationPlanExecutionMember,
    OrganizationPlanExecutionStep,
    OrganizationPlanItem,
    OrganizationPlanStatus,
    OrganizationPlanView,
    PlanSource,
)
from watch_assistant.services.organization_policy import (
    OrganizationConflictPolicy,
)
from watch_assistant.services.strm_scope import source_snapshot_is_current


class OrganizationPlanService:
    """Create and invalidate local previews without a write-capable seam."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmdb_client: TmdbMatchClient | None = None,
        *,
        event_logger: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tmdb_client = tmdb_client
        self._event_logger = event_logger

    def bind_candidate_search_client(self, tmdb_client: TmdbMatchClient) -> None:
        """Attach the read-only TMDB client after runtime credentials are loaded."""

        self._tmdb_client = tmdb_client

    async def _view_for_session(
        self, session: AsyncSession, plan: OrganizationPlan
    ) -> OrganizationPlanView:
        source_snapshot = _load_source_snapshot(plan.source_snapshot_json)
        source_snapshot_changed = await _source_snapshot_changed(
            session, plan, source_snapshot
        )
        return _view(plan, source_snapshot_changed=source_snapshot_changed)

    async def create_plan(
        self,
        *,
        library_id: str,
        scan_run_id: str,
        items: Sequence[OrganizationPlanItem],
        target_root: str = "",
        target_directory_id: str | None = None,
        target_directories: Mapping[str, str] | None = None,
        parser_version: str = "i04-v1",
        matcher_version: str = "i05-v1",
        expires_at: datetime | None = None,
        now: datetime | None = None,
        target_conflicts: Iterable[str] = (),
        organization_policy: Mapping[str, object] | None = None,
        manual_confirmation: bool = False,
    ) -> OrganizationPlanView:
        _validate_identity(library_id, "invalid_library")
        _validate_identity(scan_run_id, "invalid_scan_run")
        target_root = _validate_relative_path(target_root, allow_empty=True)
        if target_directory_id is not None:
            _validate_identity(target_directory_id, "invalid_target_directory")
        normalized_target_directories = _validate_target_directories(
            target_directories or {}
        )
        parser_version = _validate_version(parser_version)
        matcher_version = _validate_version(matcher_version)
        normalized_items = _validate_items(items)
        policy = OrganizationConflictPolicy.from_mapping(organization_policy).to_dict()
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
            # 新计划取代同库旧扫描的活跃计划:列表只保留最新一次扫描的
            # 计划,避免历史计划堆叠(用户无法分辨哪份是当前待办)。
            await session.execute(
                update(OrganizationPlan)
                .where(
                    OrganizationPlan.library_id == library_id,
                    OrganizationPlan.source_scan_run_id != run.id,
                    OrganizationPlan.status.in_(
                        (
                            OrganizationPlanStatus.NEEDS_REVIEW.value,
                            OrganizationPlanStatus.PLANNED.value,
                        )
                    ),
                )
                .values(status=OrganizationPlanStatus.INVALIDATED.value)
                .execution_options(synchronize_session=False)
            )
            rows = await self._load_source_rows(
                session, scan_run_id=run.id, items=normalized_items
            )
            source_snapshot, actions, preconditions, basis, status = _build_payload(
                normalized_items,
                rows,
                root_directory_id=library.root_directory_id,
                target_root=target_root,
                target_directory_id=target_directory_id,
                target_directories=normalized_target_directories,
                target_conflicts=target_conflicts,
                organization_policy=policy,
                source_snapshot_revision=run.snapshot_revision,
                parser_version=parser_version,
                matcher_version=matcher_version,
            )
            if manual_confirmation and status is OrganizationPlanStatus.PLANNED:
                status = OrganizationPlanStatus.NEEDS_REVIEW
            rule_versions = sorted(
                {item.naming_plan.rule_version for item in normalized_items}
            )
            if len(rule_versions) != 1:
                raise OrganizationPlanError("rule_version_conflict")
            rule_version = _validate_version(rule_versions[0])
            library_snapshot = _library_snapshot(library)
            preconditions_payload = {
                "library": library_snapshot,
                "target_directory_id": target_directory_id,
                "target_directories": normalized_target_directories,
                "organization_policy": policy,
                "items": preconditions,
            }
            # basis_json is display/audit evidence; it is intentionally excluded.
            canonical = {
                "library_id": library.id,
                "library_snapshot": library_snapshot,
                "source_snapshot": source_snapshot,
                "target_root": target_root,
                "target_directory_id": target_directory_id,
                "target_directories": normalized_target_directories,
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
                return await self._view_for_session(session, existing)
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
                return await self._view_for_session(session, existing)
            return await self._view_for_session(session, plan)

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
                return await self._view_for_session(session, plan)
            if plan.status == OrganizationPlanStatus.IGNORED.value:
                return await self._view_for_session(session, plan)
            stale = current_time >= _utc(plan.expires_at)
            library = await session.get(MediaLibrary, plan.library_id)
            stored_preconditions = _load_json_object(plan.preconditions_json)
            stored_target_directory_id = stored_preconditions.get(
                "target_directory_id"
            )
            stored_target_directories: dict[str, str] = {}
            stored_policy: dict[str, object] = OrganizationConflictPolicy().to_dict()
            try:
                if stored_target_directory_id is not None:
                    _validate_identity(
                        stored_target_directory_id, "invalid_target_directory"
                    )
                stored_target_directories = _validate_target_directories(
                    stored_preconditions.get("target_directories", {})
                )
                stored_policy = OrganizationConflictPolicy.from_mapping(
                    stored_preconditions.get("organization_policy")
                ).to_dict()
            except OrganizationPlanError:
                stale = True
                stored_target_directory_id = None
            except ValueError:
                stale = True
            if library is None or stored_preconditions.get(
                "library"
            ) != _library_snapshot(library):
                stale = True
            run = await session.get(LibraryScanRun, plan.source_scan_run_id)
            latest = await _latest_completed_scan(
                session,
                library_id=plan.library_id,
                root_directory_id=(
                    library.root_directory_id
                    if library is not None
                    else run.root_directory_id
                    if run is not None
                    else ""
                ),
            )
            if (
                library is None
                or run is None
                or latest is None
                or not _scan_is_valid_for_library(library, run)
                or not _scan_is_valid_for_library(library, latest)
                or not _plan_scan_binding_is_current(
                    plan, library=library, run=run, latest=latest
                )
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
                if (
                    library is None
                    or run is None
                    or latest is None
                    or not _plan_scan_binding_is_current(
                        plan, library=library, run=run, latest=latest
                    )
                ):
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
                        target_directory_id=stored_target_directory_id,
                        target_directories=stored_target_directories,
                        target_conflicts=target_conflicts,
                        organization_policy=stored_policy,
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
            return await self._view_for_session(session, plan)

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
        library_ids: Collection[str] | None = None,
    ) -> tuple[list[OrganizationPlanView], int | None]:
        if cursor < 0 or limit < 1 or limit > 100:
            raise OrganizationPlanError("invalid_pagination")
        async with self._session_factory() as session:
            await self._cleanup_stale_plans(session)
            statement = select(OrganizationPlan).order_by(
                OrganizationPlan.created_at.desc(), OrganizationPlan.id.desc()
            )
            if status is None:
                # 默认只展示活跃计划(待处理 + 已规划);显式传 status
                # 时仍可按任意状态过滤。
                statement = statement.where(
                    OrganizationPlan.status.in_(
                        (
                            OrganizationPlanStatus.NEEDS_REVIEW.value,
                            OrganizationPlanStatus.PLANNED.value,
                        )
                    )
                )
            else:
                statement = statement.where(OrganizationPlan.status == status.value)
            if library_ids:
                statement = statement.where(OrganizationPlan.library_id.in_(library_ids))
            rows = list(
                (await session.scalars(statement.offset(cursor).limit(limit + 1))).all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = cursor + limit if has_more else None
            views = [
                await self._view_for_session(session, row)
                for row in rows
            ]
            return views, next_cursor

    async def _cleanup_stale_plans(
        self, session: AsyncSession, *, now: datetime | None = None
    ) -> None:
        """Lazily clean up expired and long-dead plans before listing.

        Idempotent and cheap: expired active plans (``needs_review`` /
        ``planned``) are migrated to ``invalidated`` so the pending list
        drains itself, while terminal plans (``invalidated`` / ``ignored``)
        older than 7 days are deleted so history does not pile up.  Active,
        unexpired plans are never touched.
        """

        current = _utc(now)
        await session.execute(
            update(OrganizationPlan)
            .where(
                OrganizationPlan.status.in_(
                    (
                        OrganizationPlanStatus.NEEDS_REVIEW.value,
                        OrganizationPlanStatus.PLANNED.value,
                    )
                ),
                OrganizationPlan.expires_at <= current,
            )
            .values(status=OrganizationPlanStatus.INVALIDATED.value)
        )
        cutoff = current - timedelta(days=7)
        # 仍被历史/操作记录引用的计划不能删除(FK 约束),保留审计轨迹。
        stale_ids = list(
            (
                await session.scalars(
                    select(OrganizationPlan.id)
                    .where(
                        OrganizationPlan.status.in_(
                            (
                                OrganizationPlanStatus.INVALIDATED.value,
                                OrganizationPlanStatus.IGNORED.value,
                            )
                        ),
                        OrganizationPlan.created_at < cutoff,
                        ~exists(
                            select(OrganizationHistoryEntry.plan_id).where(
                                OrganizationHistoryEntry.plan_id == OrganizationPlan.id
                            )
                        ),
                        ~exists(
                            select(OrganizationOperation.plan_id).where(
                                OrganizationOperation.plan_id == OrganizationPlan.id
                            )
                        ),
                    )
                    .limit(500)
                )
            ).all()
        )
        if stale_ids:
            await session.execute(
                delete(OrganizationPlan).where(OrganizationPlan.id.in_(stale_ids))
            )
        await session.commit()

    async def get_plan(self, plan_id: str) -> OrganizationPlanView:
        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            return await self._view_for_session(session, plan)

    async def search_candidates(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        source_object_id: str | None = None,
        source_index: int | None = None,
        query: str | None = None,
        limit: int = 8,
    ) -> OrganizationPlanView:
        """Search TMDB for one review item and persist bounded evidence locally."""

        _validate_identity(plan_id, "invalid_plan")
        if expected_revision < 1:
            raise OrganizationPlanError("invalid_revision")
        if source_object_id is not None:
            _validate_identity(source_object_id, "invalid_source_object")
        if source_index is not None and (
            isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0
        ):
            raise OrganizationPlanError("invalid_source_index")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise OrganizationPlanError("invalid_candidate_limit")
        search_text = _validate_candidate_query(query)
        client = self._tmdb_client
        if client is None:
            raise OrganizationPlanError("candidate_search_unavailable")

        async with self._session_factory() as session:
            stored = await session.get(OrganizationPlan, plan_id)
            if stored is None:
                raise OrganizationPlanError("plan_not_found")
            if stored.revision != expected_revision:
                raise OrganizationPlanError("stale_revision")
            if stored.status != OrganizationPlanStatus.NEEDS_REVIEW.value:
                raise OrganizationPlanError("plan_not_reviewable")
            source_snapshot = _load_source_snapshot(stored.source_snapshot_json)
            if source_snapshot is None:
                raise OrganizationPlanError("source_snapshot_mismatch")
            selected_index = _resolve_source_index(
                source_snapshot,
                source_object_id=source_object_id,
                source_index=source_index,
            )
            if selected_index is None:
                raise OrganizationPlanError("candidate_source_required")
            selected_source = source_snapshot[selected_index]
            library = await session.get(MediaLibrary, stored.library_id)
            original_run = await session.get(LibraryScanRun, stored.source_scan_run_id)
            latest = await _latest_completed_scan(
                session,
                library_id=stored.library_id,
                root_directory_id=(
                    library.root_directory_id
                    if library is not None
                    else original_run.root_directory_id
                    if original_run is not None
                    else ""
                ),
            )
            if (
                library is None
                or original_run is None
                or latest is None
                or not _scan_is_valid_for_library(library, original_run)
                or not _scan_is_valid_for_library(library, latest)
                or not _plan_scan_binding_is_current(
                    stored, library=library, run=original_run, latest=latest
                )
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
            rows = {
                (row.object_type, row.object_id): row
                for row in await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == original_run.id
                    )
                )
            }
            row = rows.get((selected_source["object_type"], selected_source["object_id"]))
            if row is None or not _source_observation_matches(selected_source, row):
                raise OrganizationPlanError("source_snapshot_mismatch")
            basis = _load_json_list(stored.basis_json)
            evidence = _basis_for_source(basis, selected_index, selected_source)
            if evidence is None:
                raise OrganizationPlanError("source_snapshot_mismatch")
            search_text = search_text or row.name

            try:
                decision = await TmdbMatcher(client).match(
                    build_match_input(parse_media_filename(search_text))
                )
            except Exception:  # noqa: BLE001 - keep remote details private
                raise OrganizationPlanError("candidate_search_unavailable") from None
            candidates = tuple(
                ranked.candidate for ranked in decision.ranked_candidates[:limit]
            )
            changed = _merge_candidate_evidence(evidence, candidates)
            if changed:
                stored.basis_json = _json(basis)
                stored.revision += 1
                await session.commit()
            return await self._view_for_session(session, stored)

    async def select_candidate(
        self,
        plan_id: str,
        *,
        source_object_id: str,
        tmdb_id: int,
        expected_revision: int,
    ) -> OrganizationPlanView:
        """Promote an explicit TMDB choice into the same durable plan."""

        _validate_identity(plan_id, "invalid_plan")
        _validate_identity(source_object_id, "invalid_source_object")
        if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise OrganizationPlanError("invalid_tmdb_candidate")
        async with self._session_factory() as session:
            stored = await session.get(OrganizationPlan, plan_id)
            if stored is None:
                raise OrganizationPlanError("plan_not_found")
            if stored.revision != expected_revision:
                raise OrganizationPlanError("stale_revision")
            if stored.status != OrganizationPlanStatus.NEEDS_REVIEW.value:
                raise OrganizationPlanError("plan_not_reviewable")
            basis = _load_json_list(stored.basis_json)
            source_snapshot = _load_source_snapshot(stored.source_snapshot_json) or []
            selected_payload = None
            for evidence in basis:
                if not isinstance(evidence, dict):
                    continue
                evidence_source_id = evidence.get("source_object_id")
                if not isinstance(evidence_source_id, str):
                    index = evidence.get("source_index")
                    source = source_snapshot[index] if isinstance(index, int) and index < len(source_snapshot) else None
                    evidence_source_id = source.get("object_id") if isinstance(source, dict) else None
                if evidence_source_id != source_object_id:
                    continue
                selected_payload = next(
                    (
                        candidate
                        for candidate in evidence.get("candidates", [])
                        if isinstance(candidate, dict) and candidate.get("tmdb_id") == tmdb_id
                    ),
                    None,
                )
                break
            if not isinstance(selected_payload, dict):
                raise OrganizationPlanError("tmdb_candidate_not_found")
            library = await session.get(MediaLibrary, stored.library_id)
            original_run = await session.get(LibraryScanRun, stored.source_scan_run_id)
            latest = await _latest_completed_scan(
                session,
                library_id=stored.library_id,
                root_directory_id=(
                    library.root_directory_id
                    if library is not None
                    else original_run.root_directory_id
                    if original_run is not None
                    else ""
                ),
            )
            if (
                library is None
                or original_run is None
                or latest is None
                or not _scan_is_valid_for_library(library, original_run)
                or not _scan_is_valid_for_library(library, latest)
                or not _plan_scan_binding_is_current(
                    stored, library=library, run=original_run, latest=latest
                )
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
            rows = {
                (row.object_type, row.object_id): row
                for row in await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == original_run.id
                    )
                )
            }
            row = rows.get(("file", source_object_id))
            source_index = next(
                (
                    index
                    for index, source in enumerate(source_snapshot)
                    if source.get("object_id") == source_object_id
                ),
                None,
            )
            if (
                source_index is None
                or row is None
                or not isinstance(row.parent_id, str)
                or not isinstance(row.path, str)
                or not _source_observation_matches(source_snapshot[source_index], row)
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
            target_config = _load_json_object(stored.preconditions_json)
            target_directory_id = target_config.get("target_directory_id")
            target_directories = target_config.get("target_directories", {})
            policy = target_config.get("organization_policy", {})
            try:
                candidate = TmdbCandidate.from_payload(selected_payload)
            except (TypeError, ValueError):
                raise OrganizationPlanError("invalid_tmdb_candidate") from None
            decision = MatchDecision(
                status=MatchStatus.ACCEPTED,
                selected=candidate,
                confidence=MatchConfidence.HIGH,
                source=MatchSource.MANUAL,
            )
            parsed = parse_media_filename(row.name)
            naming_plan = plan_media(
                parsed,
                decision,
                rules=NamingRuleConfig(library_root=stored.target_root or ""),
            )
            if not naming_plan.executable or not naming_plan.target_path:
                raise OrganizationPlanError("candidate_target_unavailable")
            try:
                normalized_target_directories = _validate_target_directories(
                    target_directories
                )
            except OrganizationPlanError:
                raise OrganizationPlanError("candidate_target_unavailable") from None
            target_parent_id = _directory_id_for_path(
                normalized_target_directories,
                str(PurePosixPath(naming_plan.target_path).parent),
            )
            # The target directory may not contain the category sub-directories
            # yet (fresh/empty target).  Fall back to the target root: the
            # directory provisioner creates each missing sub-directory at
            # execution time.
            if target_parent_id is None and isinstance(target_directory_id, str):
                target_parent_id = target_directory_id
            if target_parent_id is None:
                raise OrganizationPlanError("candidate_target_unavailable")
            item = OrganizationPlanItem(
                source=PlanSource(
                    object_type=row.object_type,
                    object_id=row.object_id,
                    parent_id=row.parent_id,
                    path=row.path,
                    remote_version=_entry_remote_version(row),
                ),
                naming_plan=naming_plan,
                decision=decision,
                target_parent_id=target_parent_id,
                target_name=PurePosixPath(naming_plan.target_path).name,
                target_directory_path=(
                    naming_plan.target_directory
                    if naming_plan.target_path
                    else None
                ),
            )
            selected_source, selected_action, selected_precondition, selected_basis, _ = (
                _build_payload(
                    (item,),
                    rows,
                    root_directory_id=library.root_directory_id,
                    target_root=stored.target_root,
                    target_directory_id=(
                        target_directory_id
                        if isinstance(target_directory_id, str)
                        else None
                    ),
                    target_directories=normalized_target_directories,
                    target_conflicts=(),
                    organization_policy=(
                        policy if isinstance(policy, Mapping) else {}
                    ),
                    source_snapshot_revision=original_run.snapshot_revision,
                    parser_version=stored.parser_version,
                    matcher_version=stored.matcher_version,
                )
            )
            old_source_snapshot = _load_source_snapshot(stored.source_snapshot_json)
            old_actions = _load_json_list(stored.actions_json)
            old_preconditions = _load_json_object(stored.preconditions_json)
            old_precondition_items = old_preconditions.get("items")
            old_basis = _load_json_list(stored.basis_json)
            if (
                old_source_snapshot is None
                or not isinstance(old_precondition_items, list)
                or len(old_source_snapshot) != len(old_actions)
                or len(old_precondition_items) != len(old_actions)
                or source_index >= len(old_actions)
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
            old_source_snapshot[source_index] = selected_source[0]
            selected_action[0]["order"] = source_index
            if isinstance(selected_action[0].get("execution"), dict):
                selected_action[0]["execution"]["order"] = source_index
            selected_precondition[0]["source_index"] = source_index
            old_actions[source_index] = selected_action[0]
            old_precondition_items[source_index] = selected_precondition[0]
            if source_index < len(old_basis):
                selected_basis[0]["source_index"] = source_index
                old_basis[source_index] = selected_basis[0]
            else:
                old_basis.extend(selected_basis)
            _reconcile_target_conflicts(old_actions, old_precondition_items)
            status = (
                OrganizationPlanStatus.PLANNED
                if all(
                    isinstance(action, dict)
                    and action.get("kind") == "move"
                    and isinstance(action.get("execution"), dict)
                    for action in old_actions
                )
                else OrganizationPlanStatus.NEEDS_REVIEW
            )
            library_snapshot = _library_snapshot(library)
            preconditions_payload = {
                "library": library_snapshot,
                "target_directory_id": target_directory_id,
                "target_directories": normalized_target_directories,
                "organization_policy": OrganizationConflictPolicy.from_mapping(
                    policy if isinstance(policy, Mapping) else {}
                ).to_dict(),
                "items": old_precondition_items,
            }
            canonical = {
                "library_id": library.id,
                "library_snapshot": library_snapshot,
                "source_snapshot": old_source_snapshot,
                "target_root": stored.target_root,
                "target_directory_id": target_directory_id,
                "target_directories": normalized_target_directories,
                "actions": old_actions,
                "preconditions": preconditions_payload,
                "rule_version": stored.rule_version,
                "parser_version": stored.parser_version,
                "matcher_version": stored.matcher_version,
            }
            # 条件更新 CAS:并发 select_candidate 完成后另一个调用
            # 的 WHERE revision=expected 命中 0 行,防止静默覆盖。
            result = await session.execute(
                update(OrganizationPlan)
                .where(
                    OrganizationPlan.id == plan_id,
                    OrganizationPlan.revision == expected_revision,
                    OrganizationPlan.status
                    == OrganizationPlanStatus.NEEDS_REVIEW.value,
                )
                .values(
                    source_scan_run_id=original_run.id,
                    source_snapshot_revision=original_run.snapshot_revision,
                    source_snapshot_json=_json(old_source_snapshot),
                    actions_json=_json(old_actions),
                    basis_json=_json(old_basis),
                    preconditions_json=_json(preconditions_payload),
                    status=status.value,
                    plan_hash=_canonical_hash(canonical),
                    revision=expected_revision + 1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise OrganizationPlanError("stale_revision")
            await session.commit()
            refreshed = await session.get(OrganizationPlan, plan_id)
            if refreshed is None:
                raise OrganizationPlanError("plan_not_found")
            return await self._view_for_session(session, refreshed)

    async def plan_library_id(self, plan_id: str) -> str:
        """Return the owning library ID for a scope check at an adapter boundary."""

        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            return plan.library_id

    async def plan_target_directory_paths(self, plan_id: str) -> tuple[str, ...]:
        """Return target parent paths recorded by a persisted preview."""

        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationPlanError("plan_not_found")
            actions = _load_json_list(plan.actions_json)
        paths: set[str] = set()
        for action in actions:
            if not isinstance(action, dict):
                continue
            # 持久化的完整目标目录路径(如 movie/Season 02)优先;旧计划
            # 没有该字段时回退到从文件名推导的父路径。
            directory_path = action.get("target_directory_path")
            if isinstance(directory_path, str) and directory_path:
                paths.add(directory_path)
                continue
            if not isinstance(action.get("target"), str):
                continue
            parent = str(PurePosixPath(action["target"]).parent)
            if parent != ".":
                paths.add(parent)
        return tuple(sorted(paths))

    async def confirm_plan(
        self, plan_id: str, *, expected_revision: int
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
            if plan.status not in {
                OrganizationPlanStatus.NEEDS_REVIEW.value,
                OrganizationPlanStatus.PLANNED.value,
            }:
                raise OrganizationPlanError("plan_not_reviewable")
            if _utc(plan.expires_at) <= datetime.now(UTC):
                raise OrganizationPlanError("plan_expired")

        # Confirmation is an execution gate, so it must prove the same
        # durable snapshot and payload that the worker will later execute.
        steps = await load_executable_steps(
            self._session_factory,
            plan_id,
            allow_unconfirmed=True,
            expected_plan_revision=expected_revision,
        )
        if not steps:
            raise OrganizationPlanError("plan_prerequisites_changed")
        view = await self._transition_plan(
            plan_id,
            expected_revision=expected_revision,
            target=OrganizationPlanStatus.PLANNED,
            allowed=(
                OrganizationPlanStatus.NEEDS_REVIEW,
                OrganizationPlanStatus.PLANNED,
            ),
        )
        await self._audit("organize.plan.confirmed", view.status.value)
        return view

    async def invalidate_plan(self, plan_id: str) -> OrganizationPlanView | None:
        """Invalidate an active plan whose source files were removed by cleanup.

        Returns the refreshed view, or ``None`` when the plan is absent or
        already terminal.  The automation pass does not track plan revisions,
        so no revision precondition applies; the status predicate still makes
        the transition race-safe.
        """
        _validate_identity(plan_id, "invalid_plan")
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                return None
            if plan.status not in {
                OrganizationPlanStatus.NEEDS_REVIEW.value,
                OrganizationPlanStatus.PLANNED.value,
            }:
                return None
            result = await session.execute(
                update(OrganizationPlan)
                .where(
                    OrganizationPlan.id == plan_id,
                    OrganizationPlan.status.in_(
                        (
                            OrganizationPlanStatus.NEEDS_REVIEW.value,
                            OrganizationPlanStatus.PLANNED.value,
                        )
                    ),
                )
                .values(
                    status=OrganizationPlanStatus.INVALIDATED.value,
                    revision=OrganizationPlan.revision + 1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            refreshed = await session.get(OrganizationPlan, plan_id)
            if refreshed is None:
                return None
            return await self._view_for_session(session, refreshed)

    async def ignore_plan_at_revision(
        self, plan_id: str, *, expected_revision: int
    ) -> OrganizationPlanView:
        view = await self._transition_plan(
            plan_id,
            expected_revision=expected_revision,
            target=OrganizationPlanStatus.IGNORED,
            allowed=tuple(OrganizationPlanStatus),
        )
        await self._audit("organize.plan.ignored", view.status.value)
        return view

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
                return await self._view_for_session(session, plan)
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
            view = await self._view_for_session(session, refreshed)
        await self._audit("organize.plan.alias_changed", view.status.value)
        return view

    async def _audit(self, event: str, status: str) -> None:
        await audit_event(self._event_logger, event, status)

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
                return await self._view_for_session(session, plan)
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
            return await self._view_for_session(session, refreshed)

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
        if not await source_snapshot_is_current(
            session,
            library_id=library_id,
            source_scan_run_id=run.id,
            source_snapshot_revision=run.snapshot_revision,
        ):
            raise OrganizationPlanError("scan_not_current")
        checkpoint = await session.get(LibraryScanCheckpoint, run.id)
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id
                    )
                )
            ).all()
        )
        try:
            validate_complete_scan_evidence(
                run,
                checkpoint,
                entries,
                root_directory_id=library.root_directory_id,
                require_tree=True,
            )
        except LibraryIndexError:
            raise OrganizationPlanError("scan_not_current") from None
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
                    or not _source_version_matches(item.source.remote_version, row)
                )
            ):
                raise OrganizationPlanError("source_snapshot_mismatch")
        return rows


__all__ = [
    "OrganizationExecutionBlocker",
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

