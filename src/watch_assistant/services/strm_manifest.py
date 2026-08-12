"""Local, fail-closed STRM manifest generation from complete library scans."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import func, select
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
from watch_assistant.models import StrmOperationKind
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_errors import StrmManifestError
from watch_assistant.services.strm_fencing import (
    CancelCheck,
    LeaseCheck,
    SessionFence,
    _commit_fenced,
    _is_lease_error,
    _LeaseFence,
    _raise_if_cancelled,
    _raise_if_conflicting_operation,
    _raise_if_lease_lost,
    _validate_fencing,
)
from watch_assistant.services.strm_fs import (
    _FileMutation,
    _is_managed_content,
    _read_target,
    _remove_with_undo,
    _restore_file_mutations,
    _safe_prefix,
    _safe_root,
    _valid_relative_path,
    _write_with_undo,
)
from watch_assistant.services.strm_path import absolute_path as _absolute_path
from watch_assistant.services.strm_path import valid_id as _valid_id
from watch_assistant.services.strm_scope import source_snapshot_is_current

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".flv", ".m2ts", ".mkv", ".mov", ".mp4", ".ts", ".webm", ".wmv"}
)
_RECONCILE_BATCH_SIZE = 100


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


ProgressCallback = Callable[[StrmGenerationSummary], Awaitable[None]]


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
                        if str(error) == "uncertain":
                            # 提交结果不确定(补偿失败):不得折叠成普通 failed,
                            # 否则运维无法区分"可安全重试"与"必须先核对远端"。
                            # 以明确错误码向上传播,让调用方标为 UNCERTAIN。
                            raise StrmManifestError(
                                "strm_operation_uncertain"
                            ) from None
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
            # 第一趟:先 retire 全部 removed。同名替换"删除旧+同名新增"场景下,
            # added 的 collision 检查要求旧 manifest 先退出;单趟按 object_id
            # 顺序处理时,新 id 字典序更小会让 added 先到 → 必 path_collision。
            if retire_removed:
                last_removed_id: str | None = None
                while True:
                    await _raise_if_cancelled(cancel_check, lease_check)
                    removed_query = (
                        select(LibraryScanDiff)
                        .where(
                            LibraryScanDiff.scan_run_id == run_pk,
                            LibraryScanDiff.object_type == "file",
                            LibraryScanDiff.change_kind == "removed",
                        )
                        .order_by(LibraryScanDiff.object_id)
                        .limit(_RECONCILE_BATCH_SIZE)
                    )
                    if last_removed_id is not None:
                        removed_query = removed_query.where(
                            LibraryScanDiff.object_id > last_removed_id
                        )
                    removed_changes = list(
                        (await session.scalars(removed_query)).all()
                    )
                    if not removed_changes:
                        break
                    for change in removed_changes:
                        await _raise_if_cancelled(cancel_check, lease_check)
                        last_removed_id = change.object_id
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
                        # 第一趟已 retire 全部 removed(见上);此处仅防御
                        # 极端顺序残留,不再重复处理。
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
                        if str(error) == "uncertain":
                            # 提交结果不确定(补偿失败):不得折叠成普通 failed,
                            # 否则运维无法区分"可安全重试"与"必须先核对远端"。
                            # 以明确错误码向上传播,让调用方标为 UNCERTAIN。
                            raise StrmManifestError(
                                "strm_operation_uncertain"
                            ) from None
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

