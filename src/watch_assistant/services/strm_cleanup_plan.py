"""Non-destructive STRM cleanup plans built from complete scan evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmCleanupPlan,
    StrmManifestEntry,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    _commit_fenced,
    _FileMutation,
    _LeaseFence,
    _remove_with_undo,
    _restore_file_mutations,
)
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    has_newer_unsettled_scan,
    normalize_playback_url_prefix,
)


class StrmCleanupPlanError(ValueError):
    """Stable local error without paths or remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


LeaseCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class StrmCleanupPlanView:
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
        }


@dataclass(frozen=True, slots=True)
class StrmCleanupApplyView:
    plan: StrmCleanupPlanView
    retired: int


class StrmCleanupPlanService:
    """Persist reviewable candidates without retiring manifests or files."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        managed_output_roots: Collection[Path | str] = (),
    ) -> None:
        self._session_factory = session_factory
        self._managed_output_roots = tuple(
            _absolute_path(Path(root)) for root in managed_output_roots
        )

    async def create_plan(
        self,
        *,
        library_id: str,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        now: datetime | None = None,
    ) -> StrmCleanupPlanView:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmCleanupPlanError("invalid_request")
        root = _readable_root(output_root, self._managed_output_roots)
        prefix = _safe_prefix(playback_url_prefix)
        current_time = _utc(now)
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            diffs = list(
                (
                    await session.scalars(
                        select(LibraryScanDiff)
                        .where(
                            LibraryScanDiff.scan_run_id == run.id,
                            LibraryScanDiff.object_type == "file",
                            LibraryScanDiff.change_kind == "removed",
                        )
                        .order_by(LibraryScanDiff.object_id)
                    )
                ).all()
            )
            object_ids = [item.object_id for item in diffs]
            manifests = {
                item.cloud_file_id: item
                for item in (
                    await session.scalars(
                        select(StrmManifestEntry).where(
                            StrmManifestEntry.library_id == library.id,
                            StrmManifestEntry.cloud_file_id.in_(object_ids),
                            StrmManifestEntry.is_current.is_(True),
                        )
                    )
                ).all()
            } if object_ids else {}
            candidates = []
            for object_id in object_ids:
                manifest = manifests.get(object_id)
                if manifest is None:
                    continue
                state = _managed_state(
                    root,
                    manifest.local_relative_path,
                    f"{prefix}{manifest.manifest_id}\n",
                )
                candidates.append(
                    {
                        "manifest_id": manifest.manifest_id,
                        "cloud_file_id": manifest.cloud_file_id,
                        "local_relative_path": manifest.local_relative_path,
                        "state": state,
                    }
                )
            canonical = {
                "library_id": library.id,
                "source_scan_run_id": run.id,
                "source_snapshot_revision": run.snapshot_revision,
                "candidates": candidates,
            }
            plan_hash = hashlib.sha256(
                json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            existing = await session.scalar(
                select(StrmCleanupPlan).where(StrmCleanupPlan.plan_hash == plan_hash)
            )
            if existing is not None:
                return _view(existing)
            plan = StrmCleanupPlan(
                id="strm_cleanup_" + uuid.uuid4().hex,
                library_id=library.id,
                source_scan_run_id=run.id,
                source_snapshot_revision=run.snapshot_revision,
                candidates_json=json.dumps(candidates, ensure_ascii=True, sort_keys=True),
                status="needs_review",
                revision=1,
                expires_at=current_time + timedelta(hours=24),
                plan_hash=plan_hash,
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return _view(plan)

    async def get_plan(self, plan_id: str) -> StrmCleanupPlanView:
        if not _valid_id(plan_id):
            raise StrmCleanupPlanError("plan_not_found")
        async with self._session_factory() as session:
            plan = await session.get(StrmCleanupPlan, plan_id)
            if plan is None:
                raise StrmCleanupPlanError("plan_not_found")
            return _view(plan)

    async def apply_plan(
        self,
        *,
        plan_id: str,
        expected_revision: int,
        digest: str,
        confirm: bool,
        idempotency_key: str,
        output_root: Path | str,
        playback_url_prefix: str,
        now: datetime | None = None,
        lease_check: LeaseCheck | None = None,
        operation_id: str | None = None,
    ) -> StrmCleanupApplyView:
        if (
            not _valid_id(plan_id)
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or not all(char in "0123456789abcdef" for char in digest.lower())
            or not confirm
            or not _valid_id(idempotency_key)
            or (operation_id is not None and not _valid_id(operation_id))
        ):
            raise StrmCleanupPlanError("invalid_request")
        if operation_id is not None and lease_check is None:
            raise StrmCleanupPlanError("strm_operation_lease_required")
        await _raise_if_lease_lost(lease_check)
        fence = _LeaseFence(operation_id, lease_check)
        root = _readable_root(output_root, self._managed_output_roots)
        prefix = _safe_prefix(playback_url_prefix)
        current_time = _utc(now)
        async with self._session_factory() as session:
            plan = await session.get(StrmCleanupPlan, plan_id)
            if plan is None:
                raise StrmCleanupPlanError("plan_not_found")
            if not hmac.compare_digest(plan.plan_hash, digest.lower()):
                raise StrmCleanupPlanError("plan_digest_mismatch")
            if plan.status == "applied":
                if plan.applied_idempotency_key != idempotency_key:
                    raise StrmCleanupPlanError("cleanup_plan_already_applied")
                return StrmCleanupApplyView(
                    _view(plan), int(plan.applied_retired or 0)
                )
            if plan.revision != expected_revision:
                raise StrmCleanupPlanError("plan_revision_changed")
            if plan.status != "needs_review":
                raise StrmCleanupPlanError("cleanup_plan_not_reviewable")
            if _utc(plan.expires_at) <= current_time:
                raise StrmCleanupPlanError("cleanup_plan_expired")
            library, _run = await self._validated_current_run(
                session, plan.library_id, plan.source_scan_run_id
            )
            if (
                await active_strm_operation_id(
                    session, library.id, exclude_operation_id=operation_id
                )
                is not None
            ):
                raise StrmCleanupPlanError("strm_library_operation_conflict")
            await _bind_fence(fence, session)
            candidates = _candidates(plan)
            manifest_ids = [item["manifest_id"] for item in candidates]
            manifests = {
                item.manifest_id: item
                for item in (
                    await session.scalars(
                        select(StrmManifestEntry).where(
                            StrmManifestEntry.library_id == library.id,
                            StrmManifestEntry.manifest_id.in_(manifest_ids),
                            StrmManifestEntry.is_current.is_(True),
                        )
                    )
                ).all()
            } if manifest_ids else {}
            preflight: list[tuple[StrmManifestEntry, str]] = []
            for item in candidates:
                await _assert_fence_current(fence, session)
                planned_state = item["state"]
                if planned_state not in {"ready", "missing"}:
                    raise StrmCleanupPlanError("cleanup_plan_blocked")
                manifest = manifests.get(item["manifest_id"])
                if manifest is None or manifest.cloud_file_id != item["cloud_file_id"]:
                    raise StrmCleanupPlanError("cleanup_plan_changed")
                state = _managed_state(
                    root,
                    manifest.local_relative_path,
                    f"{prefix}{manifest.manifest_id}\n",
                )
                if state != planned_state:
                    raise StrmCleanupPlanError("cleanup_plan_blocked")
                preflight.append((manifest, state))
            retired = 0
            mutations: list[_FileMutation] = []
            try:
                for manifest, state in preflight:
                    await _assert_fence_current(fence, session)
                    if state == "ready":
                        try:
                            mutation = _remove_with_undo(
                                root,
                                manifest.local_relative_path,
                                f"{prefix}{manifest.manifest_id}\n".encode(),
                            )
                        except StrmManifestError:
                            raise StrmCleanupPlanError("cleanup_plan_blocked") from None
                        if mutation is not None:
                            mutations.append(mutation)
                        await _assert_fence_current(fence, session)
                    await _assert_fence_current(fence, session)
                    manifest.is_current = False
                    manifest.status = "retired"
                    retired += 1
                await _assert_fence_current(fence, session)
                plan.status = "applied"
                plan.revision += 1
                plan.applied_idempotency_key = idempotency_key
                plan.applied_retired = retired
                await self._commit_plan(
                    session,
                    fence,
                    plan,
                    idempotency_key=idempotency_key,
                    retired=retired,
                    mutations=mutations,
                )
            except asyncio.CancelledError:
                await _rollback_cleanup(session, mutations)
                raise
            except StrmManifestError as error:
                await _rollback_cleanup(session, mutations)
                code = str(error)
                if code == "strm_operation_lease_lost":
                    raise StrmCleanupPlanError(code) from None
                raise StrmCleanupPlanError("cleanup_plan_blocked") from None
            except StrmCleanupPlanError:
                await _rollback_cleanup(session, mutations)
                raise
            except SQLAlchemyError:
                await _rollback_cleanup(session, mutations)
                raise StrmCleanupPlanError("uncertain") from None
            except OSError as error:
                await _rollback_cleanup(session, mutations)
                raise StrmCleanupPlanError("uncertain") from error
            await session.refresh(plan)
            return StrmCleanupApplyView(_view(plan), retired)

    async def _commit_plan(
        self,
        session: AsyncSession,
        fence: _LeaseFence,
        plan: StrmCleanupPlan,
        *,
        idempotency_key: str,
        retired: int,
        mutations: list[_FileMutation],
    ) -> None:
        plan_id = plan.id
        plan_revision = plan.revision
        try:
            # A direct service caller may not have an operation ledger claim.
            # Keep the plan revision as the final idempotency fence so two
            # confirmations cannot both publish different terminal results.
            with session.no_autoflush:
                result = await session.execute(
                    update(StrmCleanupPlan)
                    .where(
                        StrmCleanupPlan.id == plan_id,
                        StrmCleanupPlan.status == "needs_review",
                        StrmCleanupPlan.revision == plan_revision - 1,
                    )
                    .values(
                        status="applied",
                        revision=plan_revision,
                        applied_idempotency_key=idempotency_key,
                        applied_retired=retired,
                    )
                    .execution_options(synchronize_session=False)
                )
            if result.rowcount != 1:
                raise StrmCleanupPlanError("cleanup_plan_changed")
            await _commit_fenced(session, fence)
            mutations.clear()
        except asyncio.CancelledError:
            committed = await self._recover_commit_failure(
                session,
                fence,
                plan_id=plan_id,
                revision=plan_revision,
                idempotency_key=idempotency_key,
                retired=retired,
                mutations=mutations,
                cancelled=True,
            )
            if not committed:
                raise
        except SQLAlchemyError:
            await self._recover_commit_failure(
                session,
                fence,
                plan_id=plan_id,
                revision=plan_revision,
                idempotency_key=idempotency_key,
                retired=retired,
                mutations=mutations,
                cancelled=False,
            )

    async def _recover_commit_failure(
        self,
        session: AsyncSession,
        fence: _LeaseFence,
        *,
        plan_id: str,
        revision: int,
        idempotency_key: str,
        retired: int,
        mutations: list[_FileMutation],
        cancelled: bool,
    ) -> bool:
        rollback_failed = False
        try:
            await asyncio.shield(session.rollback())
        except asyncio.CancelledError:
            rollback_failed = True
        except Exception:  # noqa: BLE001 - probe the commit outcome next
            rollback_failed = True
        observed = await self._observe_plan_commit(
            plan_id,
            revision=revision,
            idempotency_key=idempotency_key,
            retired=retired,
        )
        if observed is True:
            mutations.clear()
            return True
        lease_current = await fence.observe_database_lease(self._session_factory)
        if lease_current is False:
            try:
                _restore_file_mutations(mutations)
            except Exception:  # noqa: BLE001 - compensation failure is uncertainty
                raise StrmCleanupPlanError("uncertain") from None
            mutations.clear()
            raise StrmCleanupPlanError("strm_operation_lease_lost")
        if lease_current is None:
            raise StrmCleanupPlanError("uncertain")
        if rollback_failed:
            raise StrmCleanupPlanError("uncertain") from None
        try:
            _restore_file_mutations(mutations)
        except Exception:  # noqa: BLE001 - compensation failure is uncertainty
            raise StrmCleanupPlanError("uncertain") from None
        mutations.clear()
        if observed is None:
            raise StrmCleanupPlanError("uncertain")
        if not cancelled:
            raise StrmCleanupPlanError("uncertain")
        return False

    async def _observe_plan_commit(
        self,
        plan_id: str,
        *,
        revision: int,
        idempotency_key: str,
        retired: int,
    ) -> bool | None:
        try:
            async with self._session_factory() as session:
                plan = await session.get(StrmCleanupPlan, plan_id)
                if plan is None:
                    return False
                return bool(
                    plan.status == "applied"
                    and plan.revision == revision
                    and plan.applied_idempotency_key == idempotency_key
                    and int(plan.applied_retired or 0) == retired
                )
        except SQLAlchemyError:
            return None

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
            raise StrmCleanupPlanError("source_snapshot_not_ready")
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
            raise StrmCleanupPlanError("source_snapshot_not_current")
        if await has_newer_unsettled_scan(session, run):
            raise StrmCleanupPlanError("source_snapshot_not_current")
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
            raise StrmCleanupPlanError("source_snapshot_not_ready") from None
        return library, run


def _view(plan: StrmCleanupPlan) -> StrmCleanupPlanView:
    candidates = _candidates(plan)
    executable = sum(
        isinstance(item, dict) and item.get("state") in {"ready", "missing"}
        for item in candidates
    )
    return StrmCleanupPlanView(
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
    )


def _candidates(plan: StrmCleanupPlan) -> list[dict[str, str]]:
    try:
        candidates = json.loads(plan.candidates_json)
    except (TypeError, json.JSONDecodeError):
        raise StrmCleanupPlanError("plan_invalid") from None
    if not isinstance(candidates, list) or any(
        not isinstance(item, dict)
        or not all(
            isinstance(item.get(key), str)
            for key in (
                "manifest_id",
                "cloud_file_id",
                "local_relative_path",
                "state",
            )
        )
        for item in candidates
    ):
        raise StrmCleanupPlanError("plan_invalid")
    return candidates


def _managed_state(root: Path, relative_path: str, expected: str) -> str:
    if not _valid_relative_path(relative_path):
        return "unsafe"
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        if _has_symlink_component(target.parent):
            return "unsafe"
        target.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return "unsafe"
    if not target.exists():
        return "missing"
    if target.is_symlink() or not target.is_file():
        return "unsafe"
    try:
        actual = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "unreadable"
    return "ready" if actual == expected else "changed"


async def _raise_if_lease_lost(lease_check: LeaseCheck | None) -> None:
    if lease_check is not None and not await lease_check():
        raise StrmCleanupPlanError("strm_operation_lease_lost")


async def _bind_fence(fence: _LeaseFence, session: AsyncSession) -> None:
    try:
        await fence.bind(session)
    except StrmManifestError as error:
        code = str(error)
        if code == "strm_operation_lease_lost":
            raise StrmCleanupPlanError(code) from None
        raise StrmCleanupPlanError("cleanup_plan_blocked") from None


async def _assert_fence_current(
    fence: _LeaseFence, session: AsyncSession
) -> None:
    try:
        await fence.assert_current(session)
    except StrmManifestError as error:
        code = str(error)
        if code == "strm_operation_lease_lost":
            raise StrmCleanupPlanError(code) from None
        raise StrmCleanupPlanError("cleanup_plan_blocked") from None


async def _rollback_cleanup(
    session: AsyncSession,
    mutations: list[_FileMutation],
) -> None:
    database_error: BaseException | None = None
    try:
        await asyncio.shield(session.rollback())
    except BaseException as error:  # noqa: BLE001 - compensation must continue
        database_error = error
    file_error: BaseException | None = None
    try:
        _restore_file_mutations(mutations)
    except BaseException as error:  # noqa: BLE001 - report explicit uncertainty
        file_error = error
    if database_error is not None or file_error is not None:
        raise StrmCleanupPlanError("uncertain") from None


def _readable_root(
    value: Path | str,
    managed_output_roots: Collection[Path] = (),
) -> Path:
    root = _absolute_path(Path(value))
    if managed_output_roots and not any(
        _same_path(root, allowed) for allowed in managed_output_roots
    ):
        raise StrmCleanupPlanError("strm_output_unavailable")
    if _has_symlink_component(root):
        raise StrmCleanupPlanError("strm_output_unavailable")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise StrmCleanupPlanError("strm_output_unavailable") from error
    if not resolved.is_dir():
        raise StrmCleanupPlanError("strm_output_unavailable")
    return resolved


def _absolute_path(value: Path) -> Path:
    """Make a lexical absolute path before checking symlink components."""

    return Path(os.path.abspath(os.fspath(value)))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _safe_prefix(value: object) -> str:
    try:
        return normalize_playback_url_prefix(value)
    except ValueError:
        raise StrmCleanupPlanError("invalid_playback_url_prefix") from None


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and "/" not in value
        and "\\" not in value
    )


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and path.as_posix() == value and all(
        part not in {"", ".", ".."} and ":" not in part for part in path.parts
    )


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path.cwd()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "StrmCleanupApplyView",
    "StrmCleanupPlanError",
    "StrmCleanupPlanService",
    "StrmCleanupPlanView",
]
