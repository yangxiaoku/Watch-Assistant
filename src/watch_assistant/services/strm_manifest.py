"""Local, fail-closed STRM manifest generation from complete library scans."""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".flv", ".m2ts", ".mkv", ".mov", ".mp4", ".ts", ".webm", ".wmv"}
)


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


class StrmManifestService:
    """Generate only files represented by one complete current scan."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_current(
        self, library_id: str, *, page: int = 1, page_size: int = 50
    ) -> tuple[tuple[StrmManifestItem, ...], int]:
        if not _valid_id(library_id) or page < 1 or not 1 <= page_size <= 100:
            raise StrmManifestError("invalid_request")
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(StrmManifestEntry).where(
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
    ) -> StrmGenerationSummary:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmManifestError("invalid_request")
        prefix = _safe_prefix(playback_url_prefix)
        root = _safe_root(output_root)
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
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry)
                        .where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.object_type == "file",
                            LibraryScanEntry.is_directory.is_(False),
                        )
                        .order_by(LibraryScanEntry.object_id)
                    )
                ).all()
            )
            generated = unchanged = skipped = failed = 0
            for entry in entries:
                paths = _paths(entry)
                if paths is None:
                    skipped += 1
                    continue
                cloud_path, local_path = paths
                manifest = await session.scalar(
                    select(StrmManifestEntry).where(
                        StrmManifestEntry.library_id == library_id,
                        StrmManifestEntry.cloud_file_id == entry.object_id,
                        StrmManifestEntry.is_current.is_(True),
                    )
                )
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
                        source_version=run.snapshot_revision,
                        status=StrmManifestStatus.PENDING,
                        is_current=True,
                    )
                    session.add(manifest)
                    await session.flush()
                else:
                    manifest.cloud_directory_id = entry.parent_id
                    manifest.cloud_relative_path = cloud_path
                    manifest.local_relative_path = local_path
                    manifest.size_bytes = entry.size_bytes
                    manifest.source_version = run.snapshot_revision
                    manifest.status = StrmManifestStatus.PENDING
                try:
                    content = f"{prefix}{quote(manifest.manifest_id, safe='')}\n".encode()
                    written = _write(root, local_path, content)
                except (OSError, StrmManifestError):
                    failed += 1
                    continue
                manifest.status = StrmManifestStatus.VERIFIED
                manifest.last_verified_at = datetime.now(UTC)
                if written:
                    generated += 1
                else:
                    unchanged += 1
            await session.commit()
        return StrmGenerationSummary(library_id, source_scan_run_id, generated, unchanged, skipped, failed)


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
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("symlink_target")
    if target.is_file() and target.read_bytes() == content:
        return False
    if target.exists() and not target.is_file():
        raise StrmManifestError("target_not_file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".watch-assistant-", dir=parent)
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


def _safe_root(value: Path | str) -> Path:
    root = Path(value)
    root.mkdir(parents=True, exist_ok=True)
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
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise StrmManifestError("invalid_playback_url_prefix")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise StrmManifestError("invalid_playback_url_prefix")
    return value.rstrip("/") + "/"


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value.isascii() and "/" not in value and "\\" not in value


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and path.as_posix() == value and all(part not in {"", ".", ".."} for part in path.parts)


__all__ = ["StrmGenerationSummary", "StrmManifestError", "StrmManifestItem", "StrmManifestService"]
