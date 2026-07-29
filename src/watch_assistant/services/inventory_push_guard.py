"""Fail-closed inventory checks immediately before a remote push."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryMediaIdentity,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import Resource
from watch_assistant.services.library_inventory import (
    FreshnessStatus,
    InventoryDecision,
    InventoryFile,
    InventorySnapshot,
    build_snapshot,
    check_inventory,
)


@dataclass(frozen=True, slots=True)
class InventoryPushCheck:
    """The only result a worker needs before it may submit remotely."""

    allowed: bool
    code: str
    decision: InventoryDecision | None = None


class InventoryPushGuard:
    """Check every enabled library and fail closed on missing evidence.

    A worker may proceed only when all configured library scopes have a fresh,
    complete snapshot and the candidate is not a duplicate or an unconfirmed
    identity match.  The guard is read-only and never touches the 115 adapter.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        freshness_threshold_seconds: int = 900,
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
            review_decision: InventoryDecision | None = None
            for library in libraries:
                run = await session.scalar(
                    select(LibraryScanRun)
                    .where(LibraryScanRun.library_id == library.id)
                    .order_by(
                        LibraryScanRun.created_at.desc(), LibraryScanRun.id.desc()
                    )
                    .limit(1)
                )
                if run is None:
                    return InventoryPushCheck(False, "inventory_index_incomplete")
                snapshot = await _snapshot(session, run, library.id, self._freshness_threshold_seconds)
                status = snapshot.freshness.status
                if status is FreshnessStatus.INCOMPLETE:
                    return InventoryPushCheck(False, "inventory_index_incomplete")
                if status is FreshnessStatus.STALE:
                    return InventoryPushCheck(False, "inventory_index_stale")
                if status is FreshnessStatus.UNKNOWN:
                    return InventoryPushCheck(False, "inventory_index_unknown")
                decision = check_inventory(snapshot, **probe)
                if decision is InventoryDecision.EXACT_DUPLICATE:
                    return InventoryPushCheck(False, "inventory_exact_duplicate", decision)
                if decision in {
                    InventoryDecision.MEDIA_DUPLICATE,
                    InventoryDecision.VERSION_DUPLICATE,
                    InventoryDecision.NEEDS_REVIEW,
                }:
                    review_decision = decision
            if review_decision is not None:
                return InventoryPushCheck(
                    False, "inventory_review_required", review_decision
                )
            return InventoryPushCheck(True, "inventory_not_found", InventoryDecision.NOT_FOUND)


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


__all__ = ["InventoryPushCheck", "InventoryPushGuard"]
