"""Fail-closed inventory checks immediately before a remote push."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    DirectoryFingerprint,
    LibraryMediaIdentity,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import Resource
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.library_inventory import (
    InventoryDecision,
    InventoryFile,
    InventorySnapshot,
    build_snapshot,
    check_inventory,
)
from watch_assistant.services.strm_scope import source_snapshot_is_current

# 本地缓存 P1(2026-08-16 设计):根目录指纹的本地有效窗口。TTL 内守卫
# 纯本地判定(0 次 115 调用);过期后由刷新路径做 1 次 fs_info 指纹核对,
# 指纹未变仅更新核对时间,变化才升级全树重扫。
FINGERPRINT_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class InventoryPushCheck:
    """The only result a worker needs before it may submit remotely."""

    allowed: bool
    code: str
    decision: InventoryDecision | None = None


@dataclass(frozen=True, slots=True)
class InventoryRefreshEvidence:
    """Evidence returned by a pre-push refresh before the guard is retried."""

    complete: bool
    scope_verified: bool
    error_code: str | None = None
    library_count: int = 0
    refreshed_count: int = 0

    @property
    def usable(self) -> bool:
        return self.complete and self.scope_verified and self.error_code is None

    def __bool__(self) -> bool:
        return self.usable


class InventoryPushGuard:
    """Check every enabled library and fail closed on missing evidence.

    A worker may proceed only when all configured library scopes have a fresh,
    complete snapshot and the candidate is not an exact duplicate.  Media,
    version, and filename-candidate matches remain advisory.  The guard is
    read-only and never touches the 115 adapter.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        freshness_threshold_seconds: int = FINGERPRINT_TTL_SECONDS,
    ) -> None:
        if not 60 <= freshness_threshold_seconds <= 86_400:
            raise ValueError("invalid freshness threshold")
        self._session_factory = session_factory
        self._freshness_threshold_seconds = freshness_threshold_seconds

    async def check(self, resource_id: str) -> InventoryPushCheck:
        async with self._session_factory() as session:
            resource = await session.get(Resource, resource_id)
            if resource is None:
                return InventoryPushCheck(False, "resource_not_found")
            if resource.kind.value != "magnet":
                return InventoryPushCheck(True, "not_applicable")
            libraries = list(
                (
                    await session.scalars(
                        select(MediaLibrary)
                        .where(MediaLibrary.enabled.is_(True))
                        .order_by(MediaLibrary.id)
                    )
                ).all()
            )
            if not libraries:
                return InventoryPushCheck(False, "inventory_scope_unconfigured")

            probe = _resource_probe(resource)
            advisory_decision: InventoryDecision | None = None
            for library in libraries:
                if not library.scope_verified:
                    return InventoryPushCheck(False, "inventory_scope_unconfigured")
                run = await session.scalar(
                    select(LibraryScanRun)
                    .where(LibraryScanRun.library_id == library.id)
                    .order_by(
                        LibraryScanRun.created_at.desc(), LibraryScanRun.id.desc()
                    )
                    .limit(1)
                )
                if (
                    run is None
                    or run.root_directory_id != library.root_directory_id
                    or run.scan_mode != "tree"
                    or not run.complete
                    or run.state != "completed"
                    or run.snapshot_revision is None
                    or not await _run_covers_scope(session, run, library)
                ):
                    return InventoryPushCheck(False, "inventory_index_incomplete")
                if not await source_snapshot_is_current(
                    session,
                    library_id=library.id,
                    source_scan_run_id=run.id,
                    source_snapshot_revision=run.snapshot_revision,
                ):
                    return InventoryPushCheck(False, "inventory_index_incomplete")
                # 本地缓存 P1:新鲜判定改为目录指纹(纯本地)。指纹缺失 =
                # 本地缓存不可信(需刷新);指纹超过 TTL = 需要核对(刷新路径
                # 先做 1 次 fs_info 指纹核对,未变仅更新时间戳)。
                fingerprint = await session.get(
                    DirectoryFingerprint, (library.id, library.root_directory_id)
                )
                if fingerprint is None or fingerprint.verified_at is None:
                    return InventoryPushCheck(False, "inventory_index_incomplete")
                fingerprint_verified_at = fingerprint.verified_at
                if fingerprint_verified_at.tzinfo is None:
                    fingerprint_verified_at = fingerprint_verified_at.replace(
                        tzinfo=UTC
                    )
                if (
                    datetime.now(UTC) - fingerprint_verified_at
                ).total_seconds() > self._freshness_threshold_seconds:
                    return InventoryPushCheck(False, "inventory_index_stale")
                snapshot = await _snapshot(session, run, library.id, self._freshness_threshold_seconds)
                decision = check_inventory(snapshot, **probe)
                if decision is InventoryDecision.EXACT_DUPLICATE:
                    return InventoryPushCheck(False, "inventory_exact_duplicate", decision)
                if decision in {
                    InventoryDecision.MEDIA_DUPLICATE,
                    InventoryDecision.VERSION_DUPLICATE,
                    InventoryDecision.NEEDS_REVIEW,
                } and (
                    advisory_decision is None
                    or decision is InventoryDecision.VERSION_DUPLICATE
                    or (
                        decision is InventoryDecision.MEDIA_DUPLICATE
                        and advisory_decision is InventoryDecision.NEEDS_REVIEW
                    )
                ):
                    advisory_decision = decision
            if advisory_decision is not None:
                code_by_decision = {
                    InventoryDecision.MEDIA_DUPLICATE: "inventory_media_duplicate",
                    InventoryDecision.VERSION_DUPLICATE: "inventory_version_duplicate",
                    InventoryDecision.NEEDS_REVIEW: "inventory_review_required",
                }
                return InventoryPushCheck(
                    True, code_by_decision[advisory_decision], advisory_decision
                )
            return InventoryPushCheck(True, "inventory_not_found", InventoryDecision.NOT_FOUND)


async def library_scope_fresh(
    session: AsyncSession,
    library: MediaLibrary,
    *,
    fingerprint_ttl_seconds: int = FINGERPRINT_TTL_SECONDS,
) -> bool:
    """只读判断本地快照是否可信(0 次 115 调用)。

    本地缓存 P1(2026-08-16 设计):完整树快照 + 根目录指纹在 TTL 内 =
    本地判定可信。指纹 TTL 内即使快照超过 900s 也有效(指纹未变 =
    内容未变);指纹缺失/过期返回 False,由刷新路径做 1 次指纹核对
    或全树重扫。
    """
    run = await session.scalar(
        select(LibraryScanRun)
        .where(LibraryScanRun.library_id == library.id)
        .order_by(LibraryScanRun.created_at.desc(), LibraryScanRun.id.desc())
        .limit(1)
    )
    if (
        run is None
        or run.root_directory_id != library.root_directory_id
        or run.scan_mode != "tree"
        or not run.complete
        or run.state != "completed"
        or run.snapshot_revision is None
        or not await _run_covers_scope(session, run, library)
    ):
        return False
    if not await source_snapshot_is_current(
        session,
        library_id=library.id,
        source_scan_run_id=run.id,
        source_snapshot_revision=run.snapshot_revision,
    ):
        return False
    fingerprint = await session.get(
        DirectoryFingerprint, (library.id, library.root_directory_id)
    )
    if fingerprint is None or fingerprint.verified_at is None:
        return False
    verified_at = fingerprint.verified_at
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    return (
        datetime.now(UTC) - verified_at
    ).total_seconds() <= fingerprint_ttl_seconds


async def _run_covers_scope(
    session: AsyncSession, run: LibraryScanRun, library: MediaLibrary
) -> bool:
    """Require the same durable tree evidence used by scan consumers."""

    entries = list(
        (
            await session.scalars(
                select(LibraryScanEntry).where(LibraryScanEntry.scan_run_id == run.id)
            )
        ).all()
    )
    checkpoint = await session.get(LibraryScanCheckpoint, run.id)
    try:
        validate_complete_scan_evidence(
            run,
            checkpoint,
            entries,
            root_directory_id=library.root_directory_id,
            require_tree=True,
        )
    except LibraryIndexError:
        return False
    return True


async def _snapshot(
    session: AsyncSession,
    run: LibraryScanRun,
    library_id: str,
    freshness_threshold_seconds: int,
) -> InventorySnapshot:
    entries = list(
        (
            await session.scalars(
                select(LibraryScanEntry).where(
                    LibraryScanEntry.scan_run_id == run.id,
                    LibraryScanEntry.is_directory.is_(False),
                )
            )
        ).all()
    )
    identities = {
        identity.object_id: identity
        for identity in (
            await session.scalars(
                select(LibraryMediaIdentity).where(
                    LibraryMediaIdentity.library_id == library_id
                )
            )
        ).all()
    }
    captured_at = run.updated_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    return build_snapshot(
        (
            InventoryFile(
                object_id=entry.object_id,
                name=entry.name,
                size_bytes=entry.size_bytes,
                modified_at=entry.modified_at,
                tmdb_id=(identities[entry.object_id].tmdb_id if entry.object_id in identities else None),
                media_type=(identities[entry.object_id].media_type if entry.object_id in identities else None),
                season=(identities[entry.object_id].season if entry.object_id in identities else None),
                episode_start=(identities[entry.object_id].episode_start if entry.object_id in identities else None),
                episode_end=(identities[entry.object_id].episode_end if entry.object_id in identities else None),
            )
            for entry in entries
        ),
        complete=bool(run.complete and run.state == "completed"),
        captured_at=captured_at,
        freshness_threshold_seconds=freshness_threshold_seconds,
    )


def _resource_probe(resource: Resource) -> dict[str, object]:
    try:
        metadata = json.loads(resource.metadata_json)
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    probe: dict[str, object] = {"name": resource.name}
    for key, target in (
        ("object_id", "object_id"),
        ("content_digest", "content_digest"),
        ("infohash", "infohash"),
        ("tmdb_id", "tmdb_id"),
        ("media_type", "media_type"),
        ("season_number", "season"),
        ("episode_start", "episode_start"),
        ("episode_end", "episode_end"),
    ):
        value = metadata.get(key)
        if value is not None:
            probe[target] = value
    return probe


__all__ = [
    "InventoryPushCheck",
    "InventoryPushGuard",
    "InventoryRefreshEvidence",
    "library_scope_fresh",
]
