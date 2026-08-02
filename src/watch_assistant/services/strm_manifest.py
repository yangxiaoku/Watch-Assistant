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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    has_newer_unsettled_scan,
    normalize_playback_url_prefix,
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
ProgressCallback = Callable[[StrmGenerationSummary], Awaitable[None]]


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
            latest = await session.scalar(
                select(func.max(LibraryScanRun.snapshot_revision)).where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.complete.is_(True),
                    LibraryScanRun.state == "completed",
                )
            )
            if latest != run.snapshot_revision:
                raise StrmManifestError("source_snapshot_not_current")
            if await has_newer_unsettled_scan(session, run):
                raise StrmManifestError("source_snapshot_not_current")
            await _raise_if_conflicting_operation(
                session, library_id, operation_id=operation_id
            )
            generated = unchanged = skipped = failed = 0
            last_object_id: str | None = None
            while True:
                await _raise_if_cancelled(cancel_check, lease_check)
                query = (
                    select(LibraryScanEntry)
                    .where(
                        LibraryScanEntry.scan_run_id == run.id,
                        LibraryScanEntry.object_type == "file",
                        LibraryScanEntry.is_directory.is_(False),
                    )
                    .order_by(LibraryScanEntry.object_id)
                    .limit(_RECONCILE_BATCH_SIZE)
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
                    last_object_id = entry.object_id
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
                        async with session.begin_nested():
                            outcome = await self._reconcile_entry(
                                session,
                                library_id=library.id,
                                entry=entry,
                                source_version=run.snapshot_revision,
                                root=root,
                                prefix=prefix,
                                lease_check=lease_check,
                            )
                        await session.commit()
                    except StrmManifestError as error:
                        if _is_lease_error(error):
                            raise
                        failed += 1
                        continue
                    except OSError:
                        failed += 1
                        continue
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
            operation_id=operation_id,
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
            operation_id=operation_id,
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
        operation_id: str | None,
        progress_callback: ProgressCallback | None,
    ) -> StrmGenerationSummary:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmManifestError("invalid_request")
        _validate_fencing(operation_id, lease_check)
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
            last_object_id: str | None = None
            while True:
                await _raise_if_cancelled(cancel_check, lease_check)
                query = (
                    select(LibraryScanDiff)
                    .where(
                        LibraryScanDiff.scan_run_id == run.id,
                        LibraryScanDiff.object_type == "file",
                    )
                    .order_by(LibraryScanDiff.object_id)
                    .limit(_RECONCILE_BATCH_SIZE)
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
                            async with session.begin_nested():
                                did_retire = await self._retire_removed(
                                    session,
                                    library_id=library.id,
                                    object_id=change.object_id,
                                    root=root,
                                    prefix=prefix,
                                    lease_check=lease_check,
                                )
                            await session.commit()
                        except StrmManifestError as error:
                            if _is_lease_error(error):
                                raise
                            failed += 1
                        except OSError:
                            failed += 1
                        else:
                            retired += int(did_retire)
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
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.object_type == "file",
                            LibraryScanEntry.object_id == change.object_id,
                            LibraryScanEntry.is_directory.is_(False),
                        )
                    )
                    if entry is None:
                        failed += 1
                        continue
                    try:
                        async with session.begin_nested():
                            outcome = await self._reconcile_entry(
                                session,
                                library_id=library.id,
                                entry=entry,
                                source_version=run.snapshot_revision,
                                root=root,
                                prefix=prefix,
                                lease_check=lease_check,
                            )
                        await session.commit()
                    except StrmManifestError as error:
                        if _is_lease_error(error):
                            raise
                        failed += 1
                        continue
                    except OSError:
                        failed += 1
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
        latest = await session.scalar(
            select(func.max(LibraryScanRun.snapshot_revision)).where(
                LibraryScanRun.library_id == library_id,
                LibraryScanRun.complete.is_(True),
                LibraryScanRun.state == "completed",
            )
        )
        if latest != run.snapshot_revision:
            raise StrmManifestError("source_snapshot_not_current")
        if await has_newer_unsettled_scan(session, run):
            raise StrmManifestError("source_snapshot_not_current")
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
        lease_check: LeaseCheck | None,
    ) -> str:
        paths = _paths(entry)
        if paths is None:
            return "skipped"
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
        await _raise_if_lease_lost(lease_check)
        if manifest is None:
            manifest = StrmManifestEntry(
                manifest_id="strm_" + uuid.uuid4().hex,
                library_id=library_id,
                cloud_file_id=entry.object_id,
                cloud_directory_id=entry.parent_id,
                pickcode=None,
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
        await _raise_if_lease_lost(lease_check)
        written = False
        old_removed = False
        try:
            written = _write(root, local_path, content)
            await _raise_if_lease_lost(lease_check)
            if old_path and old_path != local_path:
                await _raise_if_lease_lost(lease_check)
                old_removed = _remove_managed(root, old_path, content)
                await _raise_if_lease_lost(lease_check)
            manifest.cloud_directory_id = entry.parent_id
            manifest.cloud_relative_path = cloud_path
            manifest.local_relative_path = local_path
            manifest.size_bytes = entry.size_bytes
            manifest.source_version = source_version
            manifest.status = StrmManifestStatus.VERIFIED
            manifest.last_verified_at = datetime.now(UTC)
            await _raise_if_lease_lost(lease_check)
        except (StrmManifestError, asyncio.CancelledError):
            if old_removed and old_path is not None:
                _write(root, old_path, content)
            if written:
                _remove_managed(root, local_path, content, tolerate_missing=True)
            raise
        return "generated" if written else "unchanged"

    async def _retire_removed(
        self,
        session: AsyncSession,
        *,
        library_id: str,
        object_id: str,
        root: Path,
        prefix: str,
        lease_check: LeaseCheck | None,
    ) -> bool:
        manifest = await session.scalar(
            select(StrmManifestEntry).where(
                StrmManifestEntry.library_id == library_id,
                StrmManifestEntry.cloud_file_id == object_id,
                StrmManifestEntry.is_current.is_(True),
            )
        )
        if manifest is None:
            return False
        expected = f"{prefix}{quote(manifest.manifest_id, safe='')}\n".encode()
        removed = False
        try:
            await _raise_if_lease_lost(lease_check)
            removed = _remove_managed(root, manifest.local_relative_path, expected)
            await _raise_if_lease_lost(lease_check)
        except (StrmManifestError, asyncio.CancelledError):
            if removed:
                _write(root, manifest.local_relative_path, expected)
            raise
        manifest.is_current = False
        manifest.status = StrmManifestStatus.RETIRED
        return True


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
    if target.is_file() and target.read_bytes() == content:
        return False
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
    return True


def _remove_managed(
    root: Path,
    relative_path: str,
    expected: bytes,
    *,
    tolerate_missing: bool = False,
) -> bool:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    _assert_no_symlink_components(target.parent)
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError as error:
        raise StrmManifestError("managed_parent_not_safe") from error
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("managed_file_not_safe")
    if not target.exists():
        if tolerate_missing:
            return False
        return False
    if not target.is_file():
        raise StrmManifestError("managed_file_not_safe")
    try:
        actual = target.read_bytes()
    except OSError as error:
        raise StrmManifestError("managed_file_not_readable") from error
    if actual != expected:
        raise StrmManifestError("managed_file_changed")
    target.unlink()
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
