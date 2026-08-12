"""Read-only verification of managed STRM files against a complete scan."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_manifest import _paths
from watch_assistant.services.strm_path import absolute_path as _absolute_path
from watch_assistant.services.strm_path import same_path as _same_path
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    normalize_playback_url_prefix,
    source_snapshot_is_current,
)


class StrmVerificationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StrmVerificationIssue:
    kind: str
    object_id: str
    manifest_id: str | None = None

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "object_id": self.object_id,
            "manifest_id": self.manifest_id,
        }


@dataclass(frozen=True, slots=True)
class StrmVerificationView:
    library_id: str
    scan_run_id: str
    snapshot_revision: int
    status: str
    checked_count: int
    valid_count: int
    missing_count: int
    invalid_count: int
    orphan_count: int
    path_mismatch_count: int
    issues: tuple[StrmVerificationIssue, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "library_id": self.library_id,
            "scan_run_id": self.scan_run_id,
            "snapshot_revision": self.snapshot_revision,
            "status": self.status,
            "checked_count": self.checked_count,
            "valid_count": self.valid_count,
            "missing_count": self.missing_count,
            "invalid_count": self.invalid_count,
            "orphan_count": self.orphan_count,
            "path_mismatch_count": self.path_mismatch_count,
            "issues": [item.to_public_dict() for item in self.issues],
        }


class StrmVerificationService:
    """Compare only system-managed STRM files; never writes or repairs."""

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

    async def verify(
        self,
        *,
        library_id: str,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        max_issues: int = 100,
    ) -> StrmVerificationView:
        if (
            not _valid_id(library_id)
            or not _valid_id(source_scan_run_id)
            or not isinstance(max_issues, int)
            or isinstance(max_issues, bool)
            or not 1 <= max_issues <= 500
        ):
            raise StrmVerificationError("invalid_request")
        root = _readable_root(output_root, self._managed_output_roots)
        prefix = _safe_prefix(playback_url_prefix)
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            if await active_strm_operation_id(session, library.id) is not None:
                raise StrmVerificationError("strm_library_operation_conflict")
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.object_type == "file",
                            LibraryScanEntry.is_directory.is_(False),
                        )
                    )
                ).all()
            )
            manifests = list(
                (
                    await session.scalars(
                        select(StrmManifestEntry).where(
                            StrmManifestEntry.library_id == library.id,
                            StrmManifestEntry.is_current.is_(True),
                        )
                    )
                ).all()
            )
        expected = {
            entry.object_id: _paths(entry)
            for entry in entries
            if _paths(entry) is not None
        }
        by_cloud = {item.cloud_file_id: item for item in manifests}
        issues: list[StrmVerificationIssue] = []
        valid_count = 0
        missing_count = 0
        invalid_count = 0
        path_mismatch_count = 0
        for object_id, paths in expected.items():
            manifest = by_cloud.get(object_id)
            if manifest is None:
                missing_count += 1
                _append_issue(
                    issues,
                    StrmVerificationIssue("missing_manifest", object_id),
                    max_issues,
                )
                continue
            if paths is not None and manifest.local_relative_path != paths[1]:
                path_mismatch_count += 1
                _append_issue(
                    issues,
                    StrmVerificationIssue(
                        "path_mismatch", object_id, manifest.manifest_id
                    ),
                    max_issues,
                )
                continue
            if manifest.status != "verified" or not _content_valid(
                root,
                manifest.local_relative_path,
                f"{prefix}{manifest.manifest_id}\n",
            ):
                invalid_count += 1
                _append_issue(
                    issues,
                    StrmVerificationIssue(
                        "invalid_content", object_id, manifest.manifest_id
                    ),
                    max_issues,
                )
                continue
            valid_count += 1
        for manifest in manifests:
            if manifest.cloud_file_id not in expected:
                _append_issue(
                    issues,
                    StrmVerificationIssue(
                        "orphan_manifest",
                        manifest.cloud_file_id,
                        manifest.manifest_id,
                    ),
                    max_issues,
                )
        orphan_count = sum(
            item.cloud_file_id not in expected for item in manifests
        )
        checked_count = len(expected)
        issue_total = missing_count + invalid_count + path_mismatch_count + orphan_count
        return StrmVerificationView(
            library_id=library.id,
            scan_run_id=run.id,
            snapshot_revision=run.snapshot_revision,
            status="verified" if issue_total == 0 else "issues",
            checked_count=checked_count,
            valid_count=valid_count,
            missing_count=missing_count,
            invalid_count=invalid_count,
            orphan_count=orphan_count,
            path_mismatch_count=path_mismatch_count,
            issues=tuple(issues),
        )

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
            raise StrmVerificationError("source_snapshot_not_ready")
        if not await source_snapshot_is_current(
            session,
            library_id=library_id,
            source_scan_run_id=run.id,
            source_snapshot_revision=run.snapshot_revision,
        ):
            raise StrmVerificationError("source_snapshot_not_current")
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
            raise StrmVerificationError("source_snapshot_not_ready") from None
        return library, run


def _append_issue(
    issues: list[StrmVerificationIssue],
    issue: StrmVerificationIssue,
    max_issues: int,
) -> None:
    if len(issues) < max_issues:
        issues.append(issue)


def _content_valid(root: Path, relative_path: str, expected: str) -> bool:
    if not _valid_relative_path(relative_path):
        return False
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        if _has_symlink_component(target.parent):
            return False
        target.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    if target.is_symlink() or not target.is_file():
        return False
    try:
        return target.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


def _readable_root(
    value: Path | str,
    managed_output_roots: Collection[Path] = (),
) -> Path:
    root = _absolute_path(Path(value))
    if managed_output_roots and not any(
        _same_path(root, allowed) for allowed in managed_output_roots
    ):
        raise StrmVerificationError("strm_output_unavailable")
    if _has_symlink_component(root):
        raise StrmVerificationError("strm_output_unavailable")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise StrmVerificationError("strm_output_unavailable") from error
    if not root.is_dir():
        raise StrmVerificationError("strm_output_unavailable")
    return root






def _safe_prefix(value: object) -> str:
    try:
        return normalize_playback_url_prefix(value)
    except ValueError:
        raise StrmVerificationError("invalid_playback_url_prefix") from None


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


__all__ = [
    "StrmVerificationError",
    "StrmVerificationIssue",
    "StrmVerificationService",
    "StrmVerificationView",
]
