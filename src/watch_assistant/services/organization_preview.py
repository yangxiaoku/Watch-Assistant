"""Build local organization previews from a verified library snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_index import ScanRunState
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingRuleConfig,
    plan_media,
)
from watch_assistant.services.media_matcher import TmdbMatcher, build_match_input
from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.organization_plan import (
    OrganizationPlanItem,
    OrganizationPlanService,
    OrganizationPlanView,
    PlanSource,
)


class OrganizationPreviewError(ValueError):
    """Stable local error without source names or remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OrganizationPreviewService:
    """Parse and match files, then persist only a local reviewable plan."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmdb_client: object,
        plan_service: OrganizationPlanService,
    ) -> None:
        self._session_factory = session_factory
        self._matcher = TmdbMatcher(tmdb_client)
        self._plan_service = plan_service

    async def create_preview(
        self,
        *,
        library_id: str,
        scan_run_id: str,
        target_directory_id: str | None = None,
        target_directories: Mapping[str, str] | None = None,
        video_extensions: Collection[str] | None = None,
        small_file_threshold_mb: float = 0.0,
        rename_enabled: bool = True,
        region_grouping_enabled: bool = True,
        target_root: str = "",
        now: datetime | None = None,
    ) -> OrganizationPlanView:
        if (
            isinstance(small_file_threshold_mb, bool)
            or not isinstance(small_file_threshold_mb, (int, float))
            or small_file_threshold_mb < 0
        ):
            raise OrganizationPreviewError("invalid_small_file_threshold")
        normalized_extensions = (
            None
            if video_extensions is None
            else {
                value.strip().lower()
                for value in video_extensions
                if isinstance(value, str) and value.strip()
            }
        )
        library, run, entries = await self._load_verified_snapshot(
            library_id, scan_run_id
        )
        files = [
            entry
            for entry in entries
            if not entry.is_directory
            and _is_video_entry(
                entry,
                video_extensions=normalized_extensions,
                small_file_threshold_mb=small_file_threshold_mb,
            )
        ]
        if not files:
            raise OrganizationPreviewError("no_video_files")

        existing_targets = tuple(
            entry.path for entry in entries if isinstance(entry.path, str) and entry.path
        )
        target_parents = (
            dict(target_directories)
            if target_directories is not None
            else _target_parent_ids(entries)
        )
        rules = NamingRuleConfig(
            library_root=target_root or "library",
            region_enabled=region_grouping_enabled,
        )
        semaphore = asyncio.Semaphore(4)

        async def build_item(entry: LibraryScanEntry) -> OrganizationPlanItem:
            parsed = parse_media_filename(entry.name)
            async with semaphore:
                decision = await self._matcher.match(build_match_input(parsed))
            naming_plan = plan_media(
                parsed,
                decision,
                existing_targets=existing_targets,
                rules=rules,
            )
            if not rename_enabled and naming_plan.target_directory:
                original_name = PurePosixPath(entry.name).name
                target_path = _join_target_path(
                    naming_plan.target_directory, original_name
                )
                if target_path is None:
                    naming_plan = replace(
                        naming_plan,
                        status=ClassificationStatus.REVIEW_REQUIRED,
                        target_path=None,
                        reasons=naming_plan.reasons + ("original_name_invalid",),
                    )
                else:
                    normalized_target = _normalize_path(target_path)
                    existing = {
                        _normalize_path(value)
                        for value in existing_targets
                        if isinstance(value, str)
                    }
                    naming_plan = replace(
                        naming_plan,
                        status=(
                            ClassificationStatus.CONFLICT
                            if normalized_target in existing
                            else naming_plan.status
                        ),
                        target_path=target_path,
                        reasons=naming_plan.reasons
                        + (("target_conflict",) if normalized_target in existing else ())
                        + ("rename_disabled",),
                    )
            target_parent_id: str | None = None
            target_name: str | None = None
            if naming_plan.target_path:
                target = PurePosixPath(naming_plan.target_path)
                target_name = target.name
                target_parent_id = target_parents.get(_normalize_path(str(target.parent)))
            path = entry.path
            if (
                not isinstance(entry.parent_id, str)
                or not entry.parent_id
                or not isinstance(path, str)
                or not path
            ):
                raise OrganizationPreviewError("scan_entry_invalid")
            return OrganizationPlanItem(
                source=PlanSource(
                    object_type=entry.object_type,
                    object_id=entry.object_id,
                    parent_id=entry.parent_id,
                    path=path,
                    remote_version=_remote_version(entry),
                    is_directory=False,
                ),
                naming_plan=naming_plan,
                decision=decision,
                target_parent_id=target_parent_id,
                target_name=target_name,
            )

        items = await asyncio.gather(*(build_item(entry) for entry in files))
        return await self._plan_service.create_plan(
            library_id=library.id,
            scan_run_id=run.id,
            items=items,
            target_directory_id=target_directory_id,
            target_directories=target_parents,
            target_root="",
            now=now,
        )

    async def _load_verified_snapshot(
        self, library_id: str, scan_run_id: str
    ) -> tuple[MediaLibrary, LibraryScanRun, list[LibraryScanEntry]]:
        async with self._session_factory() as session:
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
                raise OrganizationPreviewError("scan_not_current")
            latest = await session.scalar(
                select(LibraryScanRun)
                .where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.root_directory_id == library.root_directory_id,
                    LibraryScanRun.complete.is_(True),
                    LibraryScanRun.state == ScanRunState.COMPLETED.value,
                )
                .order_by(LibraryScanRun.snapshot_revision.desc())
                .limit(1)
            )
            if latest is None or latest.id != run.id:
                raise OrganizationPreviewError("scan_not_current")
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry)
                        .where(LibraryScanEntry.scan_run_id == run.id)
                        .order_by(LibraryScanEntry.object_type, LibraryScanEntry.object_id)
                    )
                ).all()
            )
            return library, run, entries


def _target_parent_ids(entries: Sequence[LibraryScanEntry]) -> dict[str, str]:
    by_path: dict[str, str | None] = {}
    for entry in entries:
        if not entry.is_directory or not isinstance(entry.path, str) or not entry.path:
            continue
        key = _normalize_path(entry.path)
        if key in by_path:
            by_path[key] = None
        else:
            by_path[key] = entry.object_id
    return {path: object_id for path, object_id in by_path.items() if object_id}


def _normalize_path(value: str) -> str:
    return value.strip().strip("/").replace("\\", "/")


def _is_video_entry(
    entry: LibraryScanEntry,
    *,
    video_extensions: Collection[str] | None,
    small_file_threshold_mb: float,
) -> bool:
    parsed = parse_media_filename(entry.name)
    if parsed.companion_type != "video":
        return False
    if video_extensions is not None and parsed.container not in video_extensions:
        return False
    return not (
        entry.size_bytes is not None
        and entry.size_bytes < small_file_threshold_mb * 1024 * 1024
    )


def _join_target_path(directory: str, name: str) -> str | None:
    if not isinstance(directory, str) or not directory:
        return None
    if not isinstance(name, str) or not name or name in {".", ".."}:
        return None
    if any(part in {"", ".", ".."} for part in name.replace("\\", "/").split("/")):
        return None
    return f"{directory.rstrip('/')}/{name}"


def _remote_version(entry: LibraryScanEntry) -> str:
    modified = entry.modified_at.astimezone(UTC).isoformat() if entry.modified_at else None
    payload = {
        "object_type": entry.object_type,
        "object_id": entry.object_id,
        "parent_id": entry.parent_id,
        "path": entry.path,
        "name": entry.name,
        "size_bytes": entry.size_bytes,
        "modified_at": modified,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = ["OrganizationPreviewError", "OrganizationPreviewService"]
