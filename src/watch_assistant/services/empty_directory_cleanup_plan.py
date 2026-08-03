"""Durable preview and confirmed, reversible cleanup of managed empty directories."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    EmptyDirectoryCleanupPlan,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupStatus,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    _commit_fenced,
    _LeaseFence,
)
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    has_newer_unsettled_scan,
)


class EmptyDirectoryCleanupPlanError(ValueError):
    """Stable local errors for preview and confirmed cleanup operations."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


EmptyDirectoryExecutor = Callable[
    ..., Awaitable[EmptyDirectoryCleanupStatus]
]
LeaseCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class EmptyDirectoryCleanupPlanView:
    plan_id: str
    library_id: str
    source_scan_run_id: str
    source_snapshot_revision: int
    plan_hash: str
    status: str
    revision: int
    expires_at: datetime
    candidate_count: int
    executable_count: int
    blocked_count: int
    candidates: tuple[dict[str, str], ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "library_id": self.library_id,
            "source_scan_run_id": self.source_scan_run_id,
            "source_snapshot_revision": self.source_snapshot_revision,
            "plan_hash": self.plan_hash,
            "status": self.status,
            "revision": self.revision,
            "expires_at": self.expires_at.isoformat(),
            "candidate_count": self.candidate_count,
            "executable_count": self.executable_count,
            "blocked_count": self.blocked_count,
            "candidates": [dict(item) for item in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class EmptyDirectoryCleanupApplyView:
    plan: EmptyDirectoryCleanupPlanView
    deleted: int


class EmptyDirectoryCleanupPlanService:
    """Keep remote deletion behind a durable, reviewed local plan."""

    DEFAULT_STALE_AFTER = timedelta(minutes=30)

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def create_plan(
        self,
        *,
        library_id: str,
        source_scan_run_id: str,
        protected_directory_ids: Collection[str] = (),
        system_created_directory_ids: Collection[str] = (),
        now: datetime | None = None,
    ) -> EmptyDirectoryCleanupPlanView:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise EmptyDirectoryCleanupPlanError("invalid_request")
        protected = frozenset(protected_directory_ids)
        if any(not _valid_id(item) for item in protected):
            raise EmptyDirectoryCleanupPlanError("invalid_cleanup_scope")
        system_created = frozenset(system_created_directory_ids)
        if any(not _valid_id(item) for item in system_created):
            raise EmptyDirectoryCleanupPlanError("invalid_cleanup_scope")
        current_time = _utc(now)
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.is_directory.is_(True),
                        )
                    )
                ).all()
            )
            occupied = {
                entry.parent_id
                for entry in (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.parent_id.is_not(None),
                        )
                    )
                ).all()
                if entry.parent_id is not None
            }
            candidates = [
                _candidate_from_entry(
                    entry,
                    library.root_directory_id,
                    protected,
                    occupied,
                    system_created,
                )
                for entry in sorted(entries, key=lambda item: item.object_id)
            ]
            candidates = [item for item in candidates if item is not None]
            canonical = {
                "library_id": library.id,
                "source_scan_run_id": run.id,
                "source_snapshot_revision": run.snapshot_revision,
                "protected_directory_ids": sorted(protected),
                "candidates": candidates,
            }
            plan_hash = hashlib.sha256(
                json.dumps(
                    canonical,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            existing = await session.scalar(
                select(EmptyDirectoryCleanupPlan).where(
                    EmptyDirectoryCleanupPlan.plan_hash == plan_hash
                )
            )
            if existing is not None:
                return _view(existing)
            plan = EmptyDirectoryCleanupPlan(
                id="empty_cleanup_" + uuid.uuid4().hex,
                library_id=library.id,
                source_scan_run_id=run.id,
                source_snapshot_revision=run.snapshot_revision,
                candidates_json=json.dumps(
                    candidates, ensure_ascii=True, sort_keys=True
                ),
                status="needs_review",
                revision=1,
                expires_at=current_time + timedelta(hours=24),
                plan_hash=plan_hash,
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return _view(plan)

    async def get_plan(self, plan_id: str) -> EmptyDirectoryCleanupPlanView:
        if not _valid_id(plan_id):
            raise EmptyDirectoryCleanupPlanError("plan_not_found")
        await self.recover_stale_applying()
        async with self._session_factory() as session:
            plan = await session.get(EmptyDirectoryCleanupPlan, plan_id)
            if plan is None:
                raise EmptyDirectoryCleanupPlanError("plan_not_found")
            return _view(plan)

    async def apply_plan(
        self,
        *,
        plan_id: str,
        expected_revision: int,
        digest: str,
        confirm: bool,
        idempotency_key: str,
        executor: EmptyDirectoryExecutor | None,
        system_created_directory_ids: Collection[str] = (),
        now: datetime | None = None,
        lease_check: LeaseCheck | None = None,
        operation_id: str | None = None,
    ) -> EmptyDirectoryCleanupApplyView:
        if (
            not _valid_id(plan_id)
            or type(expected_revision) is not int
            or expected_revision < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or not all(char in "0123456789abcdef" for char in digest.lower())
            or confirm is not True
            or not _valid_id(idempotency_key)
            or (operation_id is not None and not _valid_id(operation_id))
        ):
            raise EmptyDirectoryCleanupPlanError("invalid_request")
        if executor is None:
            raise EmptyDirectoryCleanupPlanError("empty_directory_cleanup_unavailable")
        if operation_id is not None and lease_check is None:
            raise EmptyDirectoryCleanupPlanError("strm_operation_lease_required")
        system_created = frozenset(system_created_directory_ids)
        if any(not _valid_id(item) for item in system_created):
            raise EmptyDirectoryCleanupPlanError("invalid_cleanup_scope")
        current_time = _utc(now)
        await _raise_if_lease_lost(lease_check)
        await self.recover_stale_applying(now=current_time)
        async with self._session_factory() as session:
            plan = await session.get(EmptyDirectoryCleanupPlan, plan_id)
            if plan is None:
                raise EmptyDirectoryCleanupPlanError("plan_not_found")
            if not hmac.compare_digest(plan.plan_hash, digest.lower()):
                raise EmptyDirectoryCleanupPlanError("plan_digest_mismatch")
            if plan.status == "applied":
                if plan.applied_idempotency_key != idempotency_key:
                    raise EmptyDirectoryCleanupPlanError(
                        "empty_cleanup_already_applied"
                    )
                return EmptyDirectoryCleanupApplyView(
                    _view(plan), int(plan.applied_deleted or 0)
                )
            if plan.status == "applying":
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_in_progress")
            if plan.revision != expected_revision:
                raise EmptyDirectoryCleanupPlanError("plan_revision_changed")
            if plan.status != "needs_review":
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_not_reviewable")
            if _utc(plan.expires_at) <= current_time:
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_expired")
            _library, run = await self._validated_current_run(
                session, plan.library_id, plan.source_scan_run_id
            )
            if (
                await active_strm_operation_id(
                    session, plan.library_id, exclude_operation_id=operation_id
                )
                is not None
            ):
                raise EmptyDirectoryCleanupPlanError(
                    "strm_library_operation_conflict"
                )
            candidates = _candidates(plan)
            ids = [item["directory_id"] for item in candidates]
            entries = {
                entry.object_id: entry
                for entry in (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.object_type == "directory",
                            LibraryScanEntry.object_id.in_(ids),
                        )
                    )
                ).all()
            } if ids else {}
            occupied = {
                entry.parent_id
                for entry in (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.parent_id.is_not(None),
                        )
                    )
                ).all()
                if entry.parent_id is not None
            }
            for candidate in candidates:
                entry = entries.get(candidate["directory_id"])
                if (
                    candidate["state"] != "ready"
                    or candidate["directory_id"] not in system_created
                    or entry is None
                    or entry.parent_id != candidate["parent_id"]
                    or entry.name != candidate["name"]
                    or candidate["directory_id"] in occupied
                ):
                    raise EmptyDirectoryCleanupPlanError("empty_cleanup_changed")

            fence = _LeaseFence(
                operation_id,
                lease_check,
                library_id=plan.library_id,
                source_scan_run_id=plan.source_scan_run_id,
                operation_kind=StrmOperationKind.CLEANUP,
                source_snapshot_revision=run.snapshot_revision,
            )
            await _bind_fence(fence, session)
            await _assert_fence_current(fence, session)
            applying_revision = expected_revision + 1
            result = await session.execute(
                update(EmptyDirectoryCleanupPlan)
                .where(
                    EmptyDirectoryCleanupPlan.id == plan_id,
                    EmptyDirectoryCleanupPlan.status == "needs_review",
                    EmptyDirectoryCleanupPlan.revision == expected_revision,
                )
                .values(status="applying", revision=applying_revision)
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(EmptyDirectoryCleanupPlan, plan_id)
                if current is not None and current.status == "applying":
                    raise EmptyDirectoryCleanupPlanError(
                        "empty_cleanup_in_progress"
                    )
                raise EmptyDirectoryCleanupPlanError("plan_revision_changed")
            try:
                await _commit_fenced(session, fence)
            except asyncio.CancelledError:
                await asyncio.shield(session.rollback())
                raise
            except StrmManifestError as error:
                await session.rollback()
                raise EmptyDirectoryCleanupPlanError(str(error)) from None
            except SQLAlchemyError:
                await asyncio.shield(session.rollback())
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None

        deleted = 0
        remote_side_effect_started = False
        try:
            for candidate in candidates:
                async with self._session_factory() as session:
                    await self._validated_current_run(
                        session, plan.library_id, plan.source_scan_run_id
                    )
                    await _assert_fence_current(fence, session)
                await _raise_if_lease_lost(lease_check)
                await self._heartbeat_claim(
                    plan_id,
                    applying_revision,
                    now=datetime.now(UTC),
                    fence=fence,
                )
                try:
                    remote_side_effect_started = True
                    result = await _execute_candidate(executor, candidate, lease_check)
                except Exception as error:  # noqa: BLE001 - remote detail stays private
                    del error
                    raise EmptyDirectoryCleanupPlanError(
                        "empty_cleanup_failed"
                    ) from None
                if result is EmptyDirectoryCleanupStatus.SUCCESS:
                    deleted += 1
                elif result is EmptyDirectoryCleanupStatus.SKIPPED:
                    raise EmptyDirectoryCleanupPlanError("empty_cleanup_changed")
                elif result is EmptyDirectoryCleanupStatus.UNCERTAIN:
                    raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain")
                else:
                    raise EmptyDirectoryCleanupPlanError("empty_cleanup_failed")
                await self._heartbeat_claim(
                    plan_id,
                    applying_revision,
                    now=datetime.now(UTC),
                    fence=fence,
                )
            async with self._session_factory() as session:
                await self._validated_current_run(
                    session, plan.library_id, plan.source_scan_run_id
                )
                await _assert_fence_current(fence, session)
            await _raise_if_lease_lost(lease_check)
        except asyncio.CancelledError:
            await self._invalidate_after_failure(
                plan_id,
                applying_revision,
                fence=fence,
                lease_lost=False,
            )
            if remote_side_effect_started:
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None
            raise
        except EmptyDirectoryCleanupPlanError as error:
            lease_lost = error.code == "strm_operation_lease_lost"
            if lease_lost and remote_side_effect_started:
                raise EmptyDirectoryCleanupPlanError(
                    "empty_cleanup_uncertain"
                ) from None
            await self._invalidate_after_failure(
                plan_id,
                applying_revision,
                fence=fence,
                lease_lost=lease_lost,
            )
            raise
        except SQLAlchemyError:
            await self._invalidate_after_failure(
                plan_id,
                applying_revision,
                fence=fence,
                lease_lost=False,
            )
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None

        async with self._session_factory() as session:
            plan = await session.get(EmptyDirectoryCleanupPlan, plan_id)
            if plan is None:
                raise EmptyDirectoryCleanupPlanError("plan_not_found")
            if plan.status != "applying" or plan.revision != applying_revision:
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_in_progress")
            plan.status = "applied"
            plan.revision += 1
            plan.applied_idempotency_key = idempotency_key
            plan.applied_deleted = deleted
            await self._commit_applied(
                session,
                fence,
                plan_id=plan_id,
                revision=plan.revision,
                idempotency_key=idempotency_key,
                deleted=deleted,
            )
            await session.refresh(plan)
            return EmptyDirectoryCleanupApplyView(_view(plan), deleted)

    async def _invalidate_claim(
        self,
        plan_id: str,
        applying_revision: int,
        *,
        fence: _LeaseFence | None = None,
    ) -> None:
        async with self._session_factory() as session:
            plan = await session.get(EmptyDirectoryCleanupPlan, plan_id)
            if (
                plan is not None
                and plan.status == "applying"
                and plan.revision == applying_revision
            ):
                plan.status = "invalidated"
                plan.revision += 1
                await _commit_fenced(
                    session,
                    fence or _LeaseFence(None, None),
                    check_source_snapshot=False,
                )

    async def _heartbeat_claim(
        self,
        plan_id: str,
        applying_revision: int,
        *,
        now: datetime,
        fence: _LeaseFence | None = None,
    ) -> None:
        current_time = _utc(now)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EmptyDirectoryCleanupPlan)
                .where(
                    EmptyDirectoryCleanupPlan.id == plan_id,
                    EmptyDirectoryCleanupPlan.status == "applying",
                    EmptyDirectoryCleanupPlan.revision == applying_revision,
                )
                .values(updated_at=current_time)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_in_progress")
            try:
                await _commit_fenced(session, fence or _LeaseFence(None, None))
            except StrmManifestError as error:
                await session.rollback()
                raise EmptyDirectoryCleanupPlanError(str(error)) from None
            except SQLAlchemyError:
                await asyncio.shield(session.rollback())
                raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None

    async def _invalidate_after_failure(
        self,
        plan_id: str,
        applying_revision: int,
        *,
        fence: _LeaseFence,
        lease_lost: bool,
    ) -> None:
        if lease_lost:
            return
        try:
            await asyncio.shield(
                self._invalidate_claim(
                    plan_id, applying_revision, fence=fence
                )
            )
        except StrmManifestError as error:
            if str(error) == "strm_operation_lease_lost":
                raise EmptyDirectoryCleanupPlanError(str(error)) from None
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None
        except SQLAlchemyError:
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None

    async def _commit_applied(
        self,
        session: AsyncSession,
        fence: _LeaseFence,
        *,
        plan_id: str,
        revision: int,
        idempotency_key: str,
        deleted: int,
    ) -> None:
        try:
            await _commit_fenced(session, fence)
        except asyncio.CancelledError:
            await asyncio.shield(session.rollback())
            if await self._observe_plan_commit(
                plan_id,
                revision=revision,
                idempotency_key=idempotency_key,
                deleted=deleted,
            ):
                return
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None
        except StrmManifestError:
            await asyncio.shield(session.rollback())
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None
        except SQLAlchemyError:
            await asyncio.shield(session.rollback())
            observed = await self._observe_plan_commit(
                plan_id,
                revision=revision,
                idempotency_key=idempotency_key,
                deleted=deleted,
            )
            if observed:
                return
            raise EmptyDirectoryCleanupPlanError("empty_cleanup_uncertain") from None

    async def _observe_plan_commit(
        self,
        plan_id: str,
        *,
        revision: int,
        idempotency_key: str,
        deleted: int,
    ) -> bool:
        try:
            async with self._session_factory() as session:
                plan = await session.get(EmptyDirectoryCleanupPlan, plan_id)
                return bool(
                    plan is not None
                    and plan.status == "applied"
                    and plan.revision == revision
                    and plan.applied_idempotency_key == idempotency_key
                    and int(plan.applied_deleted or 0) == deleted
                )
        except SQLAlchemyError:
            return False

    async def recover_stale_applying(
        self,
        *,
        max_age: timedelta | None = None,
        now: datetime | None = None,
    ) -> int:
        """Invalidate abandoned claims without replaying any remote write."""

        stale_after = self.DEFAULT_STALE_AFTER if max_age is None else max_age
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise EmptyDirectoryCleanupPlanError("invalid_cleanup_timeout")
        current_time = _utc(now)
        cutoff = current_time - stale_after
        async with self._session_factory() as session:
            plans = list(
                (
                    await session.scalars(
                        select(EmptyDirectoryCleanupPlan).where(
                            EmptyDirectoryCleanupPlan.status == "applying"
                        )
                    )
                ).all()
            )
            recovered = 0
            for plan in plans:
                if _utc(plan.updated_at) > cutoff:
                    continue
                if await _has_live_cleanup_operation(
                    session, plan.library_id, current_time
                ):
                    continue
                result = await session.execute(
                    update(EmptyDirectoryCleanupPlan)
                    .where(
                        EmptyDirectoryCleanupPlan.id == plan.id,
                        EmptyDirectoryCleanupPlan.status == "applying",
                        EmptyDirectoryCleanupPlan.revision == plan.revision,
                        EmptyDirectoryCleanupPlan.updated_at == plan.updated_at,
                    )
                    .values(
                        status="invalidated",
                        revision=plan.revision + 1,
                        updated_at=current_time,
                    )
                )
                recovered += int(result.rowcount == 1)
            if recovered:
                await session.commit()
            else:
                await session.rollback()
            return recovered

    async def _validated_current_run(
        self, session: AsyncSession, library_id: str, scan_run_id: str
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
            or run.state != "completed"
            or not run.complete
            or run.snapshot_revision is None
        ):
            raise EmptyDirectoryCleanupPlanError("source_snapshot_not_ready")
        latest = await session.scalar(
            select(LibraryScanRun.snapshot_revision)
            .where(
                LibraryScanRun.library_id == library_id,
                LibraryScanRun.complete.is_(True),
                LibraryScanRun.state == "completed",
            )
            .order_by(LibraryScanRun.snapshot_revision.desc())
            .limit(1)
        )
        if latest != run.snapshot_revision:
            raise EmptyDirectoryCleanupPlanError("source_snapshot_not_current")
        if await has_newer_unsettled_scan(session, run):
            raise EmptyDirectoryCleanupPlanError("source_snapshot_not_current")
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
            raise EmptyDirectoryCleanupPlanError("source_snapshot_not_ready") from None
        return library, run


def _candidate_from_entry(
    entry: LibraryScanEntry,
    root_directory_id: str,
    protected: Collection[str],
    occupied: Collection[str | None],
    system_created: Collection[str],
) -> dict[str, str] | None:
    if (
        entry.object_id == root_directory_id
        or entry.object_id in protected
        or entry.parent_id is None
        or entry.object_id in occupied
    ):
        return None
    path = entry.path or ""
    safe_path = _safe_relative_path(path)
    state = (
        "ready"
        if (
            entry.object_id in system_created
            and _safe_name(entry.name)
            and _valid_id(entry.parent_id)
            and safe_path
        )
        else "blocked"
    )
    return {
        "directory_id": entry.object_id,
        "parent_id": entry.parent_id,
        "name": entry.name,
        "path": path if safe_path else "",
        "state": state,
    }


def _view(plan: EmptyDirectoryCleanupPlan) -> EmptyDirectoryCleanupPlanView:
    candidates = _candidates(plan)
    executable = sum(item["state"] == "ready" for item in candidates)
    return EmptyDirectoryCleanupPlanView(
        plan_id=plan.id,
        library_id=plan.library_id,
        source_scan_run_id=plan.source_scan_run_id,
        source_snapshot_revision=plan.source_snapshot_revision,
        plan_hash=plan.plan_hash,
        status=plan.status,
        revision=plan.revision,
        expires_at=plan.expires_at,
        candidate_count=len(candidates),
        executable_count=executable,
        blocked_count=len(candidates) - executable,
        candidates=tuple(dict(item) for item in candidates),
    )


def _candidates(plan: EmptyDirectoryCleanupPlan) -> list[dict[str, str]]:
    try:
        candidates = json.loads(plan.candidates_json)
    except (TypeError, json.JSONDecodeError):
        raise EmptyDirectoryCleanupPlanError("empty_cleanup_plan_invalid") from None
    required = ("directory_id", "parent_id", "name", "path", "state")
    if not isinstance(candidates, list) or any(
        not isinstance(item, dict)
        or not all(isinstance(item.get(key), str) for key in required)
        for item in candidates
    ):
        raise EmptyDirectoryCleanupPlanError("empty_cleanup_plan_invalid")
    return candidates


def _safe_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 255
        and value not in {".", ".."}
        and not any(char in value for char in ("/", "\\", "\x00"))
    )


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        return False
    return all(part not in {"", ".", ".."} for part in normalized.split("/"))


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and "/" not in value
        and "\\" not in value
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


async def _execute_candidate(
    executor: EmptyDirectoryExecutor,
    candidate: dict[str, str],
    lease_check: LeaseCheck | None,
) -> EmptyDirectoryCleanupStatus:
    if lease_check is None or not _accepts_lease_check(executor):
        return await executor(candidate)
    return await executor(candidate, lease_check=lease_check)


def _accepts_lease_check(executor: EmptyDirectoryExecutor) -> bool:
    try:
        parameters = inspect.signature(executor).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "lease_check"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _raise_if_lease_lost(lease_check: LeaseCheck | None) -> None:
    if lease_check is not None and not await lease_check():
        raise EmptyDirectoryCleanupPlanError("strm_operation_lease_lost")


async def _bind_fence(fence: _LeaseFence, session: AsyncSession) -> None:
    try:
        await fence.bind(session)
    except StrmManifestError as error:
        raise EmptyDirectoryCleanupPlanError(str(error)) from None


async def _assert_fence_current(
    fence: _LeaseFence, session: AsyncSession
) -> None:
    try:
        await fence.assert_current(session)
    except StrmManifestError as error:
        raise EmptyDirectoryCleanupPlanError(str(error)) from None


async def _has_live_cleanup_operation(
    session: AsyncSession, library_id: str, now: datetime
) -> bool:
    operation_id = await session.scalar(
        select(StrmOperation.id)
        .where(
            StrmOperation.library_id == library_id,
            StrmOperation.kind == StrmOperationKind.CLEANUP,
            StrmOperation.status == StrmOperationStatus.RUNNING,
            StrmOperation.lease_expires_at.is_not(None),
            StrmOperation.lease_expires_at > now,
        )
        .limit(1)
    )
    return operation_id is not None


__all__ = [
    "EmptyDirectoryCleanupApplyView",
    "EmptyDirectoryCleanupPlanError",
    "EmptyDirectoryCleanupPlanService",
    "EmptyDirectoryCleanupPlanView",
]
