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
    CompanionFile,
    NamingRuleConfig,
    plan_media,
)
from watch_assistant.services.media_matcher import TmdbMatcher, build_match_input
from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.organization_plan import (
    OrganizationPlanCompanion,
    OrganizationPlanItem,
    OrganizationPlanService,
    OrganizationPlanView,
    PlanSource,
)
from watch_assistant.services.organization_policy import (
    OrganizationConflictPolicy,
    VersionDecision,
    compare_versions,
    evidence_from_parse,
)
from watch_assistant.services.organization_target import OrganizationTargetFile


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
        source_directory_id: str | None = None,
        target_directory_id: str | None = None,
        target_directories: Mapping[str, str] | None = None,
        existing_target_files: Sequence[OrganizationTargetFile] = (),
        video_extensions: Collection[str] | None = None,
        metadata_extensions: Collection[str] | None = None,
        small_file_threshold_mb: float = 0.0,
        rename_enabled: bool = True,
        region_grouping_enabled: bool = True,
        year_grouping_enabled: bool = False,
        include_children_category: bool = False,
        include_concert_category: bool = False,
        media_probe_enabled: bool = False,
        ai_identification_enabled: bool = False,
        cleanup_empty_directories: bool = False,
        strm_linkage_enabled: bool = False,
        prefer_remux: bool = True,
        prefer_resolution: bool = True,
        prefer_dolby: bool = False,
        conflict_mode: int = 2,
        multi_version_enabled: bool = False,
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
        normalized_metadata_extensions = (
            None
            if metadata_extensions is None
            else {
                value.strip().lower()
                for value in metadata_extensions
                if isinstance(value, str) and value.strip()
            }
        )
        library, run, entries = await self._load_verified_snapshot(
            library_id, scan_run_id
        )
        source_entries = entries
        if source_directory_id is not None:
            if (
                not isinstance(source_directory_id, str)
                or not source_directory_id
                or len(source_directory_id) > 128
                or "/" in source_directory_id
                or "\\" in source_directory_id
                or "\x00" in source_directory_id
            ):
                raise OrganizationPreviewError("source_directory_not_found")
            directory_ids = {
                entry.object_id
                for entry in entries
                if entry.is_directory and isinstance(entry.object_id, str)
            }
            if source_directory_id != library.root_directory_id and source_directory_id not in directory_ids:
                raise OrganizationPreviewError("source_directory_not_found")
            descendants = {source_directory_id}
            changed = True
            while changed:
                changed = False
                for entry in entries:
                    if (
                        entry.is_directory
                        and isinstance(entry.object_id, str)
                        and entry.object_id not in descendants
                        and entry.parent_id in descendants
                    ):
                        descendants.add(entry.object_id)
                        changed = True
            source_entries = [
                entry
                for entry in entries
                if entry.is_directory or entry.parent_id in descendants
            ]
        files = [
            entry
            for entry in source_entries
            if not entry.is_directory
            and _is_video_entry(
                entry,
                video_extensions=normalized_extensions,
                small_file_threshold_mb=small_file_threshold_mb,
            )
        ]
        if not files:
            raise OrganizationPreviewError("no_video_files")

        policy = OrganizationConflictPolicy(
            prefer_remux=prefer_remux,
            prefer_resolution=prefer_resolution,
            prefer_dolby=prefer_dolby,
            conflict_mode=conflict_mode,
            multi_version_enabled=multi_version_enabled,
            media_probe_enabled=media_probe_enabled,
            ai_identification_enabled=ai_identification_enabled,
            cleanup_empty_directories=cleanup_empty_directories,
            strm_linkage_enabled=strm_linkage_enabled,
        )
        existing_files_by_path = {
            _normalize_path(entry.path): entry
            for entry in entries
            if not entry.is_directory
            and isinstance(entry.path, str)
            and entry.path
        }
        for target_file in existing_target_files:
            if not isinstance(target_file, OrganizationTargetFile):
                raise OrganizationPreviewError("target_file_invalid")
            existing_files_by_path[_normalize_path(target_file.path)] = target_file
        target_parents = (
            dict(target_directories)
            if target_directories is not None
            else _target_parent_ids(entries)
        )
        rules = NamingRuleConfig(
            library_root=target_root or "library",
            region_enabled=region_grouping_enabled,
            year_grouping_enabled=year_grouping_enabled,
            include_children_category=include_children_category,
            include_concert_category=include_concert_category,
            preserve_technical_tags=media_probe_enabled,
        )
        semaphore = asyncio.Semaphore(4)

        async def build_item(entry: LibraryScanEntry) -> OrganizationPlanItem:
            parsed = parse_media_filename(entry.name)
            companion_entries = _find_companion_entries(
                entry,
                source_entries,
                metadata_extensions=normalized_metadata_extensions,
            )
            companion_files = tuple(
                CompanionFile(item.object_id, parse_media_filename(item.name))
                for item in companion_entries
            )
            async with semaphore:
                decision = await self._matcher.match(build_match_input(parsed))
            naming_plan = plan_media(
                parsed,
                decision,
                # Existing targets are evaluated below by the version policy;
                # plan_media alone cannot know which version should win.
                existing_targets=(),
                rules=rules,
                companions=companion_files,
            )
            policy_decision: VersionDecision | None = None
            policy_evidence = evidence_from_parse(parsed, size_bytes=entry.size_bytes)
            existing_evidence = None
            replacement_object_id: str | None = None
            replacement_parent_id: str | None = None
            replacement_name: str | None = None
            if naming_plan.target_path:
                target_key = _normalize_path(naming_plan.target_path)
                existing_entry = existing_files_by_path.get(target_key)
                if existing_entry is not None and existing_entry.object_id != entry.object_id:
                    existing_parsed = parse_media_filename(existing_entry.name)
                    existing_evidence = evidence_from_parse(
                        existing_parsed, size_bytes=existing_entry.size_bytes
                    )
                    policy_decision = compare_versions(
                        policy_evidence, existing_evidence, policy
                    )
                    replacement_allowed = policy_decision.outcome == "candidate"
                    naming_plan = replace(
                        naming_plan,
                        status=(
                            ClassificationStatus.PLANNED
                            if replacement_allowed
                            else ClassificationStatus.CONFLICT
                        ),
                        reasons=naming_plan.reasons
                        + (
                            "target_conflict",
                            f"policy_{policy_decision.reason}",
                            "replacement_planned"
                            if replacement_allowed
                            else "replacement_not_planned",
                        ),
                    )
                    if replacement_allowed:
                        replacement_object_id = existing_entry.object_id
                        replacement_parent_id = existing_entry.parent_id
                        replacement_name = existing_entry.name
            if ai_identification_enabled and not decision.accepted:
                naming_plan = replace(
                    naming_plan,
                    reasons=naming_plan.reasons + ("ai_identification_unavailable",),
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
                        for value in existing_files_by_path
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
            mapping_by_key = {
                mapping.association_key: mapping
                for mapping in naming_plan.companion_mappings
            }
            companions = tuple(
                OrganizationPlanCompanion(
                    source=PlanSource(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        parent_id=item.parent_id,
                        path=item.path,
                        remote_version=_remote_version(item),
                        is_directory=False,
                    ),
                    target_parent_id=target_parent_id,
                    target_name=mapping_by_key[item.object_id].target_name
                    if item.object_id in mapping_by_key
                    else None,
                )
                for item in companion_entries
                if isinstance(item.parent_id, str)
                and isinstance(item.path, str)
            )
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
                companions=companions,
                policy_decision=policy_decision,
                policy_evidence=policy_evidence,
                existing_evidence=existing_evidence,
                replacement_object_id=replacement_object_id,
                replacement_parent_id=replacement_parent_id,
                replacement_name=replacement_name,
            )

        items = await asyncio.gather(*(build_item(entry) for entry in files))
        return await self._plan_service.create_plan(
            library_id=library.id,
            scan_run_id=run.id,
            items=items,
            target_directory_id=target_directory_id,
            target_directories=target_parents,
            organization_policy=policy.to_dict(),
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


def _find_companion_entries(
    primary: LibraryScanEntry,
    entries: Sequence[LibraryScanEntry],
    *,
    metadata_extensions: Collection[str] | None,
) -> tuple[LibraryScanEntry, ...]:
    if metadata_extensions is None:
        return ()
    primary_parsed = parse_media_filename(primary.name)
    primary_stem = (primary_parsed.title or PurePosixPath(primary.name).stem).casefold()
    result: list[LibraryScanEntry] = []
    for entry in entries:
        if (
            entry.object_id == primary.object_id
            or entry.is_directory
            or entry.parent_id != primary.parent_id
            or not isinstance(entry.name, str)
        ):
            continue
        parsed = parse_media_filename(entry.name)
        if parsed.container not in metadata_extensions or parsed.companion_type == "unknown":
            continue
        companion_parsed = parsed
        companion_stem = (companion_parsed.title or PurePosixPath(entry.name).stem).casefold()
        if companion_stem == primary_stem or companion_stem.startswith(primary_stem + "."):
            result.append(entry)
    return tuple(sorted(result, key=lambda item: item.object_id))


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
