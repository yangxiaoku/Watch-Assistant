"""Local, fail-closed STRM manifest generation from complete library scans."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    normalize_playback_url_prefix,
    source_snapshot_is_current,
)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".flv", ".m2ts", ".mkv", ".mov", ".mp4", ".ts", ".webm", ".wmv"}
)
_RECONCILE_BATCH_SIZE = 100


class StrmManifestError(ValueError):
    """Stable local STRM contract error."""


@dataclass(frozen=True, slots=True)
class StrmManifestItem:
    manifest_id: str
    library_id: str
    cloud_file_id: str
    cloud_relative_path: str
    local_relative_path: str
    status: str
    source_version: int


@dataclass(frozen=True, slots=True)
class StrmGenerationSummary:
    library_id: str
    scan_run_id: str
    generated: int
    unchanged: int
    skipped: int
    failed: int
    retired: int = 0


CancelCheck = Callable[[], Awaitable[bool]]
LeaseCheck = Callable[[], Awaitable[bool]]
SessionFence = Callable[[AsyncSession], Awaitable[bool]]
ProgressCallback = Callable[[StrmGenerationSummary], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _FileMutation:
    """The exact file state needed to compensate one local side effect."""

    root: Path
    relative_path: str
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True, slots=True)
class _ManifestCommitExpectation:
    manifest_id: str
    cloud_file_id: str
    local_relative_path: str
    source_version: int
    status: str
    is_current: bool


@dataclass(frozen=True, slots=True)
class _ReconcileEntryResult:
    outcome: str
    expectation: _ManifestCommitExpectation | None


@dataclass(frozen=True, slots=True)
class _RetireResult:
    retired: bool
    expectation: _ManifestCommitExpectation | None


class _LeaseFence:
    """Bind a callback lease to a durable conditional database write."""

    def __init__(
        self,
        operation_id: str | None,
        lease_check: LeaseCheck | None,
        *,
        library_id: str | None = None,
        source_scan_run_id: str | None = None,
        operation_kind: StrmOperationKind | str | None = None,
        durable_fence: SessionFence | None = None,
        source_snapshot_revision: int | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.lease_check = lease_check
        self.library_id = library_id
        self.source_scan_run_id = source_scan_run_id
        self.operation_kind = (
            StrmOperationKind(operation_kind)
            if operation_kind is not None
            else None
        )
        self.durable_fence = durable_fence
        self.source_snapshot_revision = source_snapshot_revision
        self._lease_owner: str | None = None
        self._database_lease = False

    async def bind(self, session: AsyncSession) -> None:
        await _raise_if_lease_lost(self.lease_check)
        if self.operation_id is not None:
            operation = await session.get(StrmOperation, self.operation_id)
            if (
                operation is None
                or not _operation_lease_is_current(operation)
                or not self._operation_matches_scope(operation)
            ):
                raise StrmManifestError("strm_operation_lease_lost")
            self._lease_owner = operation.lease_owner
            self._database_lease = True
        await _raise_if_lease_lost(self.lease_check)

    async def assert_current(self, session: AsyncSession) -> None:
        await _raise_if_lease_lost(self.lease_check)
        if (
            self.source_snapshot_revision is not None
            and self.library_id is not None
            and self.source_scan_run_id is not None
            and not await source_snapshot_is_current(
                session,
                library_id=self.library_id,
                source_scan_run_id=self.source_scan_run_id,
                source_snapshot_revision=self.source_snapshot_revision,
            )
        ):
            raise StrmManifestError("source_snapshot_not_current")
        if self.durable_fence is not None and not await self.durable_fence(session):
            raise StrmManifestError("strm_operation_lease_lost")
        if not self._database_lease or self.operation_id is None:
            return
        operation = await session.scalar(
            select(StrmOperation)
            .where(StrmOperation.id == self.operation_id)
            .execution_options(populate_existing=True)
        )
        if (
            operation is None
            or not _operation_lease_is_current(
                operation, expected_owner=self._lease_owner
            )
            or not self._operation_matches_scope(operation)
        ):
            raise StrmManifestError("strm_operation_lease_lost")

    async def fence_commit(
        self,
        session: AsyncSession,
        *,
        check_source_snapshot: bool = True,
    ) -> None:
        """Acquire the lease row's write lock immediately before commit.

        The conditional update and the manifest transaction commit are one
        database transaction.  A competing lease takeover therefore cannot
        commit between this check and the manifest commit.
        """

        await _raise_if_lease_lost(self.lease_check)
        if self.durable_fence is not None and not await self.durable_fence(session):
            raise StrmManifestError("strm_operation_lease_lost")
        if not self._database_lease or self.operation_id is None:
            if self.source_snapshot_revision is not None:
                # Force pending manifest/plan changes into this transaction so
                # the source check and the terminal commit have one ordering.
                await session.flush()
            if (
                check_source_snapshot
                and self.source_snapshot_revision is not None
                and self.library_id is not None
                and self.source_scan_run_id is not None
                and not await source_snapshot_is_current(
                    session,
                    library_id=self.library_id,
                    source_scan_run_id=self.source_scan_run_id,
                    source_snapshot_revision=self.source_snapshot_revision,
                )
            ):
                raise StrmManifestError("source_snapshot_not_current")
            return
        result = await session.execute(
            update(StrmOperation)
            .where(
                StrmOperation.id == self.operation_id,
                StrmOperation.status == StrmOperationStatus.RUNNING,
                StrmOperation.lease_owner == self._lease_owner,
                StrmOperation.lease_expires_at.is_not(None),
                StrmOperation.lease_expires_at > datetime.now(UTC),
                *self._scope_predicates(),
            )
            # A no-op UPDATE still takes the database row/write lock without
            # changing the externally visible lease value.
            .values(heartbeat_at=StrmOperation.heartbeat_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise StrmManifestError("strm_operation_lease_lost")
        if (
            check_source_snapshot
            and self.source_snapshot_revision is not None
            and self.library_id is not None
            and self.source_scan_run_id is not None
            and not await source_snapshot_is_current(
                session,
                library_id=self.library_id,
                source_scan_run_id=self.source_scan_run_id,
                source_snapshot_revision=self.source_snapshot_revision,
            )
        ):
            raise StrmManifestError("source_snapshot_not_current")

    async def observe_database_lease(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> bool | None:
        if not self._database_lease or self.operation_id is None:
            return True
        try:
            async with session_factory() as session:
                operation = await session.get(StrmOperation, self.operation_id)
                return bool(
                    operation is not None
                    and _operation_lease_is_current(
                        operation, expected_owner=self._lease_owner
                    )
                    and self._operation_matches_scope(operation)
                )
        except SQLAlchemyError:
            return None

    def _scope_predicates(self) -> tuple[object, ...]:
        predicates: list[object] = []
        if self.library_id is not None:
            predicates.append(StrmOperation.library_id == self.library_id)
        if self.source_scan_run_id is not None:
            predicates.append(
                StrmOperation.source_scan_run_id == self.source_scan_run_id
            )
        if self.operation_kind is not None:
            predicates.append(StrmOperation.kind == self.operation_kind)
        return tuple(predicates)

    def _operation_matches_scope(self, operation: StrmOperation) -> bool:
        return bool(
            (self.library_id is None or operation.library_id == self.library_id)
            and (
                self.source_scan_run_id is None
                or operation.source_scan_run_id == self.source_scan_run_id
            )
            and (
                self.operation_kind is None or operation.kind is self.operation_kind
            )
        )


async def _commit_fenced(
    session: AsyncSession,
    fence: _LeaseFence,
    *,
    check_source_snapshot: bool = True,
) -> None:
    await fence.fence_commit(
        session, check_source_snapshot=check_source_snapshot
    )
    await session.commit()


class StrmManifestService:
    """Generate only files represented by one complete current scan."""

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

    async def _commit_entry(
        self,
        session: AsyncSession,
        fence: _LeaseFence,
        expectation: _ManifestCommitExpectation | None,
        mutations: list[_FileMutation],
    ) -> None:
        try:
            await _commit_fenced(session, fence)
            mutations.clear()
        except asyncio.CancelledError:
            committed = await self._recover_commit_failure(
                session,
                fence,
                expectation,
                mutations,
                cancelled=True,
            )
            if not committed:
                raise
        except SQLAlchemyError:
            await self._recover_commit_failure(
                session,
                fence,
                expectation,
                mutations,
                cancelled=False,
            )

    async def _recover_commit_failure(
        self,
        session: AsyncSession,
        fence: _LeaseFence,
        expectation: _ManifestCommitExpectation | None,
        mutations: list[_FileMutation],
        *,
        cancelled: bool,
    ) -> bool:
        rollback_failed = False
        try:
            await asyncio.shield(session.rollback())
        except asyncio.CancelledError:
            rollback_failed = True
        except Exception:  # noqa: BLE001 - probe the commit outcome next
            rollback_failed = True
            # A fresh read below is the only safe way to distinguish a commit
            # that failed from a commit whose acknowledgement was lost.
        observed = await self._observe_manifest_commit(expectation)
        if observed is True:
            mutations.clear()
            return True
        lease_current = await fence.observe_database_lease(self._session_factory)
        if lease_current is False:
            try:
                _restore_file_mutations(mutations)
            except Exception:  # noqa: BLE001 - compensation failure is uncertainty
                raise StrmManifestError("uncertain") from None
            mutations.clear()
            raise StrmManifestError("strm_operation_lease_lost")
        if lease_current is None:
            raise StrmManifestError("uncertain")
        if rollback_failed:
            raise StrmManifestError("uncertain") from None
        try:
            _restore_file_mutations(mutations)
        except Exception:  # noqa: BLE001 - compensation failure is uncertainty
            raise StrmManifestError("uncertain") from None
        mutations.clear()
        if observed is None:
            raise StrmManifestError("uncertain")
        if not cancelled:
            raise StrmManifestError("strm_operation_failed")
        return False

    async def _observe_manifest_commit(
        self,
        expectation: _ManifestCommitExpectation | None,
    ) -> bool | None:
        if expectation is None:
            return False
        try:
            async with self._session_factory() as session:
                row = await session.get(StrmManifestEntry, expectation.manifest_id)
                if row is None:
                    return False
                return bool(
                    row.cloud_file_id == expectation.cloud_file_id
                    and row.local_relative_path == expectation.local_relative_path
                    and row.source_version == expectation.source_version
                    and str(row.status) == expectation.status
                    and row.is_current is expectation.is_current
                )
        except SQLAlchemyError:
            return None

    async def list_current(
        self, library_id: str, *, page: int = 1, page_size: int = 50
    ) -> tuple[tuple[StrmManifestItem, ...], int]:
        if not _valid_id(library_id) or page < 1 or not 1 <= page_size <= 100:
            raise StrmManifestError("invalid_request")
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(StrmManifestEntry)
                .where(
                    StrmManifestEntry.library_id == library_id,
                    StrmManifestEntry.is_current.is_(True),
                )
            )
            rows = list(
                (
                    await session.scalars(
                        select(StrmManifestEntry)
                        .where(
                            StrmManifestEntry.library_id == library_id,
                            StrmManifestEntry.is_current.is_(True),
                        )
                        .order_by(StrmManifestEntry.manifest_id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                ).all()
            )
        return tuple(_item(row) for row in rows), int(total or 0)

    async def generate(
        self,
        library_id: str,
        *,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        cancel_check: CancelCheck | None = None,
        lease_check: LeaseCheck | None = None,
        durable_fence: SessionFence | None = None,
        operation_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> StrmGenerationSummary:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmManifestError("invalid_request")
        _validate_fencing(operation_id, lease_check)
        await _raise_if_lease_lost(lease_check)
        prefix = _safe_prefix(playback_url_prefix)
        root = _safe_root(output_root, self._managed_output_roots)
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            await _raise_if_conflicting_operation(
                session, library_id, operation_id=operation_id
            )
            fence = _LeaseFence(
                operation_id,
                lease_check,
                library_id=library_id,
                source_scan_run_id=source_scan_run_id,
                operation_kind=StrmOperationKind.FULL,
                durable_fence=durable_fence,
                source_snapshot_revision=run.snapshot_revision,
            )
            await fence.bind(session)
            library_pk = library.id
            run_pk = run.id
            source_version = run.snapshot_revision
            generated = unchanged = skipped = failed = 0
            last_object_id: str | None = None
            while True:
                await _raise_if_cancelled(cancel_check, lease_check)
                query = (
                    select(LibraryScanEntry)
                    .where(
                        LibraryScanEntry.scan_run_id == run_pk,
                        LibraryScanEntry.object_type == "file",
                        LibraryScanEntry.is_directory.is_(False),
                    )
                    .order_by(LibraryScanEntry.object_id)
                    .limit(_RECONCILE_BATCH_SIZE)
                    .execution_options(populate_existing=True)
                )
                if last_object_id is not None:
                    query = query.where(
                        LibraryScanEntry.object_id > last_object_id
                    )
                entries = list((await session.scalars(query)).all())
                if not entries:
                    break
                for entry in entries:
                    await _raise_if_cancelled(cancel_check, lease_check)
                    entry_id = entry.object_id
                    last_object_id = entry_id
                    paths = _paths(entry)
                    if paths is None:
                        skipped += 1
                        await _report_progress(
                            progress_callback,
                            library_id,
                            source_scan_run_id,
                            generated,
                            unchanged,
                            skipped,
                            failed,
                            0,
                        )
                        continue
                    try:
                        mutations: list[_FileMutation] = []
                        result = await self._reconcile_entry(
                            session,
                            library_id=library_pk,
                            entry=entry,
                            source_version=source_version,
                            root=root,
                            prefix=prefix,
                            fence=fence,
                            mutations=mutations,
                        )
                        await self._commit_entry(
                            session, fence, result.expectation, mutations
                        )
                        outcome = result.outcome
                    except asyncio.CancelledError:
                        await _rollback_entry(session, mutations)
                        raise
                    except SQLAlchemyError:
                        await _rollback_entry(session, mutations)
                        raise StrmManifestError("strm_operation_failed") from None
                    except StrmManifestError as error:
                        await _rollback_entry(session, mutations)
                        if _is_lease_error(error):
                            raise
                        failed += 1
                        break
                    except OSError:
                        await _rollback_entry(session, mutations)
                        failed += 1
                        break
                    if outcome == "generated":
                        generated += 1
                    else:
                        unchanged += 1
                    await _report_progress(
                        progress_callback,
                        library_id,
                        source_scan_run_id,
                        generated,
                        unchanged,
                        skipped,
                        failed,
                        0,
                    )
        await _raise_if_lease_lost(lease_check)
        return StrmGenerationSummary(
            library_id, source_scan_run_id, generated, unchanged, skipped, failed
        )

    async def incremental(
        self,
        library_id: str,
        *,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        retire_removed: bool = True,
        cancel_check: CancelCheck | None = None,
        lease_check: LeaseCheck | None = None,
        durable_fence: SessionFence | None = None,
        operation_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> StrmGenerationSummary:
        """Reconcile only file-level changes from one complete current scan."""

        return await self._reconcile(
            library_id,
            source_scan_run_id=source_scan_run_id,
            output_root=output_root,
            playback_url_prefix=playback_url_prefix,
            include_generation=True,
            retire_removed=retire_removed,
            cancel_check=cancel_check,
            lease_check=lease_check,
            durable_fence=durable_fence,
            operation_id=operation_id,
            operation_kind=StrmOperationKind.INCREMENTAL,
            progress_callback=progress_callback,
        )

    async def cleanup(
        self,
        library_id: str,
        *,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        cancel_check: CancelCheck | None = None,
        lease_check: LeaseCheck | None = None,
        durable_fence: SessionFence | None = None,
        operation_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> StrmGenerationSummary:
        """Retire only manifest entries removed by a complete current scan."""

        return await self._reconcile(
            library_id,
            source_scan_run_id=source_scan_run_id,
            output_root=output_root,
            playback_url_prefix=playback_url_prefix,
            include_generation=False,
            retire_removed=True,
            cancel_check=cancel_check,
            lease_check=lease_check,
            durable_fence=durable_fence,
            operation_id=operation_id,
            operation_kind=StrmOperationKind.CLEANUP,
            progress_callback=progress_callback,
        )

    async def _reconcile(
        self,
        library_id: str,
        *,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        include_generation: bool,
        retire_removed: bool,
        cancel_check: CancelCheck | None,
        lease_check: LeaseCheck | None,
        durable_fence: SessionFence | None,
        operation_id: str | None,
        operation_kind: StrmOperationKind,
        progress_callback: ProgressCallback | None,
    ) -> StrmGenerationSummary:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmManifestError("invalid_request")
        _validate_fencing(operation_id, lease_check)
        await _raise_if_lease_lost(lease_check)
        prefix = _safe_prefix(playback_url_prefix)
        root = _safe_root(output_root, self._managed_output_roots)
        generated = unchanged = skipped = failed = retired = 0
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            await _raise_if_conflicting_operation(
                session, library_id, operation_id=operation_id
            )
            fence = _LeaseFence(
                operation_id,
                lease_check,
                library_id=library_id,
                source_scan_run_id=source_scan_run_id,
                operation_kind=operation_kind,
                durable_fence=durable_fence,
                source_snapshot_revision=run.snapshot_revision,
            )
            await fence.bind(session)
            library_pk = library.id
            run_pk = run.id
            source_version = run.snapshot_revision
            last_object_id: str | None = None
            while True:
                await _raise_if_cancelled(cancel_check, lease_check)
                query = (
                    select(LibraryScanDiff)
                    .where(
                        LibraryScanDiff.scan_run_id == run_pk,
                        LibraryScanDiff.object_type == "file",
                    )
                    .order_by(LibraryScanDiff.object_id)
                    .limit(_RECONCILE_BATCH_SIZE)
                    .execution_options(populate_existing=True)
                )
                if last_object_id is not None:
                    query = query.where(LibraryScanDiff.object_id > last_object_id)
                changes = list((await session.scalars(query)).all())
                if not changes:
                    break
                for change in changes:
                    await _raise_if_cancelled(cancel_check, lease_check)
                    last_object_id = change.object_id
                    if change.change_kind == "removed":
                        if not retire_removed:
                            continue
                        try:
                            mutations = []
                            retire_result = await self._retire_removed(
                                session,
                                library_id=library_pk,
                                object_id=change.object_id,
                                root=root,
                                prefix=prefix,
                                fence=fence,
                                mutations=mutations,
                            )
                            await self._commit_entry(
                                session,
                                fence,
                                retire_result.expectation,
                                mutations,
                            )
                        except asyncio.CancelledError:
                            await _rollback_entry(session, mutations)
                            raise
                        except SQLAlchemyError:
                            await _rollback_entry(session, mutations)
                            raise StrmManifestError("strm_operation_failed") from None
                        except StrmManifestError as error:
                            await _rollback_entry(session, mutations)
                            if _is_lease_error(error):
                                raise
                            failed += 1
                            break
                        except OSError:
                            await _rollback_entry(session, mutations)
                            failed += 1
                            break
                        else:
                            retired += int(retire_result.retired)
                        await _report_progress(
                            progress_callback,
                            library_id,
                            source_scan_run_id,
                            generated,
                            unchanged,
                            skipped,
                            failed,
                            retired,
                        )
                        continue
                    if not include_generation:
                        continue
                    entry = await session.scalar(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run_pk,
                            LibraryScanEntry.object_type == "file",
                            LibraryScanEntry.object_id == change.object_id,
                            LibraryScanEntry.is_directory.is_(False),
                        )
                        .execution_options(populate_existing=True)
                    )
                    if entry is None:
                        failed += 1
                        continue
                    try:
                        mutations = []
                        result = await self._reconcile_entry(
                            session,
                            library_id=library_pk,
                            entry=entry,
                            source_version=source_version,
                            root=root,
                            prefix=prefix,
                            fence=fence,
                            mutations=mutations,
                        )
                        await self._commit_entry(
                            session, fence, result.expectation, mutations
                        )
                        outcome = result.outcome
                    except asyncio.CancelledError:
                        await _rollback_entry(session, mutations)
                        raise
                    except SQLAlchemyError:
                        await _rollback_entry(session, mutations)
                        raise StrmManifestError("strm_operation_failed") from None
                    except StrmManifestError as error:
                        await _rollback_entry(session, mutations)
                        if _is_lease_error(error):
                            raise
                        failed += 1
                        break
                    except OSError:
                        await _rollback_entry(session, mutations)
                        failed += 1
                        break
                    else:
                        if outcome == "generated":
                            generated += 1
                        elif outcome == "unchanged":
                            unchanged += 1
                        else:
                            skipped += 1
                    await _report_progress(
                        progress_callback,
                        library_id,
                        source_scan_run_id,
                        generated,
                        unchanged,
                        skipped,
                        failed,
                        retired,
                    )
        await _raise_if_lease_lost(lease_check)
        return StrmGenerationSummary(
            library_id,
            source_scan_run_id,
            generated,
            unchanged,
            skipped,
            failed,
            retired,
        )

    async def _validated_current_run(
        self, session: AsyncSession, library_id: str, source_scan_run_id: str
    ) -> tuple[MediaLibrary, LibraryScanRun]:
        library = await session.get(MediaLibrary, library_id)
        run = await session.get(LibraryScanRun, source_scan_run_id)
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
            raise StrmManifestError("source_snapshot_not_ready")
        if not await source_snapshot_is_current(
            session,
            library_id=library_id,
            source_scan_run_id=run.id,
            source_snapshot_revision=run.snapshot_revision,
        ):
            raise StrmManifestError("source_snapshot_not_current")
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
            raise StrmManifestError("source_snapshot_not_ready") from None
        return library, run

    async def _reconcile_entry(
        self,
        session: AsyncSession,
        *,
        library_id: str,
        entry: LibraryScanEntry,
        source_version: int,
        root: Path,
        prefix: str,
        fence: _LeaseFence,
        mutations: list[_FileMutation],
    ) -> _ReconcileEntryResult:
        paths = _paths(entry)
        if paths is None:
            return _ReconcileEntryResult("skipped", None)
        cloud_path, local_path = paths
        manifest = await session.scalar(
            select(StrmManifestEntry).where(
                StrmManifestEntry.library_id == library_id,
                StrmManifestEntry.cloud_file_id == entry.object_id,
                StrmManifestEntry.is_current.is_(True),
            )
        )
        collision = await session.scalar(
            select(StrmManifestEntry).where(
                StrmManifestEntry.library_id == library_id,
                StrmManifestEntry.local_relative_path == local_path,
                StrmManifestEntry.is_current.is_(True),
                StrmManifestEntry.cloud_file_id != entry.object_id,
            )
        )
        if collision is not None:
            raise StrmManifestError("path_collision")
        await fence.assert_current(session)
        if manifest is None:
            manifest = StrmManifestEntry(
                manifest_id="strm_" + uuid.uuid4().hex,
                library_id=library_id,
                cloud_file_id=entry.object_id,
                cloud_directory_id=entry.parent_id,
                pickcode=entry.pickcode,
                cloud_relative_path=cloud_path,
                local_relative_path=local_path,
                size_bytes=entry.size_bytes,
                source_version=source_version,
                status=StrmManifestStatus.PENDING,
                is_current=True,
            )
            session.add(manifest)
            await session.flush()
            old_path = None
        else:
            old_path = manifest.local_relative_path
        content = f"{prefix}{quote(manifest.manifest_id, safe='')}\n".encode()
        existing_target = _read_target(root, local_path)
        if (
            existing_target is not None
            and existing_target != content
            and (
                old_path != local_path
                or not _is_managed_content(existing_target, manifest.manifest_id)
            )
        ):
            raise StrmManifestError("managed_file_changed")
        await fence.assert_current(session)
        written, mutation = _write_with_undo(root, local_path, content)
        if mutation is not None:
            mutations.append(mutation)
        await fence.assert_current(session)
        if old_path and old_path != local_path:
            await fence.assert_current(session)
            mutation = _remove_with_undo(root, old_path, content)
            if mutation is not None:
                mutations.append(mutation)
            await fence.assert_current(session)
        manifest.cloud_directory_id = entry.parent_id
        manifest.pickcode = entry.pickcode
        manifest.cloud_relative_path = cloud_path
        manifest.local_relative_path = local_path
        manifest.size_bytes = entry.size_bytes
        manifest.source_version = source_version
        manifest.status = StrmManifestStatus.VERIFIED
        manifest.last_verified_at = datetime.now(UTC)
        await fence.assert_current(session)
        return _ReconcileEntryResult(
            "generated" if written else "unchanged",
            _ManifestCommitExpectation(
                manifest.manifest_id,
                manifest.cloud_file_id,
                manifest.local_relative_path,
                manifest.source_version,
                StrmManifestStatus.VERIFIED.value,
                True,
            ),
        )

    async def _retire_removed(
        self,
        session: AsyncSession,
        *,
        library_id: str,
        object_id: str,
        root: Path,
        prefix: str,
        fence: _LeaseFence,
        mutations: list[_FileMutation],
    ) -> _RetireResult:
        manifest = await session.scalar(
            select(StrmManifestEntry).where(
                StrmManifestEntry.library_id == library_id,
                StrmManifestEntry.cloud_file_id == object_id,
                StrmManifestEntry.is_current.is_(True),
            )
        )
        if manifest is None:
            return _RetireResult(False, None)
        expected = f"{prefix}{quote(manifest.manifest_id, safe='')}\n".encode()
        await fence.assert_current(session)
        mutation = _remove_with_undo(root, manifest.local_relative_path, expected)
        if mutation is not None:
            mutations.append(mutation)
        await fence.assert_current(session)
        manifest.is_current = False
        manifest.status = StrmManifestStatus.RETIRED
        return _RetireResult(
            True,
            _ManifestCommitExpectation(
                manifest.manifest_id,
                manifest.cloud_file_id,
                manifest.local_relative_path,
                manifest.source_version,
                StrmManifestStatus.RETIRED.value,
                False,
            ),
        )


def _item(row: StrmManifestEntry) -> StrmManifestItem:
    return StrmManifestItem(
        row.manifest_id,
        row.library_id,
        row.cloud_file_id,
        row.cloud_relative_path,
        row.local_relative_path,
        str(row.status),
        row.source_version,
    )


def _paths(entry: LibraryScanEntry) -> tuple[str, str] | None:
    value = entry.path or entry.name
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() not in VIDEO_EXTENSIONS
    ):
        return None
    local = str(path.with_suffix(".strm"))
    if not _valid_relative_path(local):
        return None
    return value, local


def _write(root: Path, relative_path: str, content: bytes) -> bool:
    written, _mutation = _write_with_undo(root, relative_path, content)
    return written


def _write_with_undo(
    root: Path, relative_path: str, content: bytes
) -> tuple[bool, _FileMutation | None]:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    parent = target.parent
    _assert_no_symlink_components(parent)
    _within(root, parent.resolve(strict=False))
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(parent)
    resolved_parent = parent.resolve(strict=True)
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("symlink_target")
    before: bytes | None = None
    if target.is_file():
        try:
            before = target.read_bytes()
        except OSError as error:
            raise StrmManifestError("managed_file_not_readable") from error
        if before == content:
            return False, None
    if target.exists() and not target.is_file():
        raise StrmManifestError("target_not_file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".watch-assistant-", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True, _FileMutation(root, relative_path, before, content)


def _remove_with_undo(
    root: Path,
    relative_path: str,
    expected: bytes,
    *,
    tolerate_missing: bool = False,
) -> _FileMutation | None:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    _assert_no_symlink_components(target.parent)
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError as error:
        if tolerate_missing and not target.exists():
            return None
        raise StrmManifestError("managed_parent_not_safe") from error
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("managed_file_not_safe")
    if not target.exists():
        return None
    if not target.is_file():
        raise StrmManifestError("managed_file_not_safe")
    try:
        actual = target.read_bytes()
    except OSError as error:
        raise StrmManifestError("managed_file_not_readable") from error
    if actual != expected:
        raise StrmManifestError("managed_file_changed")
    target.unlink()
    return _FileMutation(root, relative_path, actual, None)


def _remove_managed(
    root: Path,
    relative_path: str,
    expected: bytes,
    *,
    tolerate_missing: bool = False,
) -> bool:
    return (
        _remove_with_undo(
            root,
            relative_path,
            expected,
            tolerate_missing=tolerate_missing,
        )
        is not None
    )


def _restore_file_mutation(mutation: _FileMutation) -> None:
    current = _read_target(mutation.root, mutation.relative_path)
    if mutation.after is None:
        if current is None:
            _write(mutation.root, mutation.relative_path, mutation.before or b"")
        elif current != mutation.before:
            raise StrmManifestError("uncertain")
        return
    if current is not None and current != mutation.after:
        raise StrmManifestError("uncertain")
    if mutation.before is None:
        if current is not None:
            _remove_managed(
                mutation.root,
                mutation.relative_path,
                mutation.after,
                tolerate_missing=True,
            )
    else:
        _write(mutation.root, mutation.relative_path, mutation.before)


def _restore_file_mutations(mutations: list[_FileMutation]) -> None:
    first_error: BaseException | None = None
    for mutation in reversed(mutations):
        try:
            _restore_file_mutation(mutation)
        except BaseException as error:  # noqa: BLE001 - finish every compensation
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise StrmManifestError("uncertain") from None


async def _rollback_entry(
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
        raise StrmManifestError("uncertain") from None


def _read_target(root: Path, relative_path: str) -> bytes | None:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    _assert_no_symlink_components(target.parent)
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError:
        if not target.exists():
            return None
        raise StrmManifestError("managed_parent_not_safe") from None
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("managed_file_not_safe")
    if not target.exists():
        return None
    if not target.is_file():
        raise StrmManifestError("managed_file_not_safe")
    try:
        return target.read_bytes()
    except OSError as error:
        raise StrmManifestError("managed_file_not_readable") from error


def _is_managed_content(content: bytes, manifest_id: str) -> bool:
    """Recognize a prior stable playback entry for this manifest only."""

    if not content.endswith(b"\n") or b"\n" in content[:-1] or b"\r" in content:
        return False
    try:
        value = content[:-1].decode("ascii")
    except UnicodeDecodeError:
        return False
    marker = quote(manifest_id, safe="")
    if not value.endswith(marker):
        return False
    try:
        normalize_playback_url_prefix(value[: -len(marker)])
    except ValueError:
        return False
    return True


def _safe_root(
    value: Path | str,
    managed_output_roots: Collection[Path] = (),
) -> Path:
    root = _absolute_path(Path(value))
    _assert_no_symlink_components(root)
    if managed_output_roots and not any(
        _same_path(root, allowed) for allowed in managed_output_roots
    ):
        raise StrmManifestError("output_root_not_allowed")
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(root)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise StrmManifestError("output_root_not_directory")
    return resolved


def _within(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise StrmManifestError("output_path_escapes_root") from error


def _safe_prefix(value: object) -> str:
    try:
        return normalize_playback_url_prefix(value)
    except ValueError:
        raise StrmManifestError("invalid_playback_url_prefix") from None


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and "/" not in value
        and "\\" not in value
    )


def _valid_relative_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(
            part not in {"", ".", ".."} and ":" not in part
            for part in path.parts
        )
    )


def _absolute_path(value: Path) -> Path:
    """Make a lexical absolute path without following symlinks."""

    return Path(os.path.abspath(os.fspath(value)))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path.cwd()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise StrmManifestError("symlink_path_component")


async def _raise_if_cancelled(
    cancel_check: CancelCheck | None,
    lease_check: LeaseCheck | None,
) -> None:
    await _raise_if_lease_lost(lease_check)
    if cancel_check is not None and await cancel_check():
        raise StrmManifestError("strm_operation_cancelled")


def _operation_lease_is_current(
    operation: StrmOperation,
    *,
    expected_owner: str | None = None,
) -> bool:
    if (
        operation.status is not StrmOperationStatus.RUNNING
        or operation.lease_owner is None
        or operation.lease_expires_at is None
    ):
        return False
    if expected_owner is not None and operation.lease_owner != expected_owner:
        return False
    expires_at = operation.lease_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


async def _raise_if_lease_lost(lease_check: LeaseCheck | None) -> None:
    if lease_check is not None and not await lease_check():
        raise StrmManifestError("strm_operation_lease_lost")


async def _raise_if_conflicting_operation(
    session: AsyncSession,
    library_id: str,
    *,
    operation_id: str | None,
) -> None:
    if operation_id is not None and not _valid_id(operation_id):
        raise StrmManifestError("invalid_request")
    active_id = await active_strm_operation_id(
        session, library_id, exclude_operation_id=operation_id
    )
    if active_id is not None:
        raise StrmManifestError("strm_library_operation_conflict")


def _is_lease_error(error: StrmManifestError) -> bool:
    return str(error) == "strm_operation_lease_lost"


def _validate_fencing(
    operation_id: str | None,
    lease_check: LeaseCheck | None,
) -> None:
    if operation_id is not None and lease_check is None:
        raise StrmManifestError("strm_operation_lease_required")


async def _report_progress(
    progress_callback: ProgressCallback | None,
    library_id: str,
    source_scan_run_id: str,
    generated: int,
    unchanged: int,
    skipped: int,
    failed: int,
    retired: int,
) -> None:
    if progress_callback is None:
        return
    await progress_callback(
        StrmGenerationSummary(
            library_id,
            source_scan_run_id,
            generated,
            unchanged,
            skipped,
            failed,
            retired,
        )
    )


__all__ = [
    "StrmGenerationSummary",
    "StrmManifestError",
    "StrmManifestItem",
    "StrmManifestService",
]
