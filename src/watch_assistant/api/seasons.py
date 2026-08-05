"""Authenticated season metadata and completeness routes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from watch_assistant.library_models import (
    LibraryMediaIdentity,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.schemas import (
    EpisodeCompletenessItemResponse,
    EpisodeCompletenessResponse,
    SeasonDetailResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.episode_completeness import (
    EpisodeBaseline,
    EpisodeFileReference,
    build_episode_matrix,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    validate_complete_scan_evidence,
)
from watch_assistant.services.library_inventory import (
    InventoryFile,
    build_snapshot,
)
from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.season_metadata import (
    SeasonMetadataError,
    SeasonMetadataService,
)
from watch_assistant.services.strm_scope import source_snapshot_is_current

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_season_service(request: Request) -> SeasonMetadataService:
    service = getattr(request.app.state, "season_metadata_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="season_metadata_unavailable")
    return service


SeasonServiceDependency = Annotated[
    SeasonMetadataService, Depends(get_season_service)
]


@router.get(
    "/media/tv/{tmdb_id}/seasons/{season_number}",
    response_model=SeasonDetailResponse,
)
async def get_season_metadata(
    tmdb_id: int,
    season_number: int,
    service: SeasonServiceDependency,
    language: str = Query(default="zh-CN", min_length=2, max_length=32),
    fallback_language: str = Query(
        default="en-US", min_length=2, max_length=32
    ),
    refresh: bool = Query(default=False),
) -> SeasonDetailResponse:
    try:
        return await service.get(
            tmdb_id,
            season_number,
            language=language,
            fallback_language=fallback_language,
            refresh=refresh,
        )
    except SeasonMetadataError as exc:
        status = 404 if exc.code == "season_not_found" else 422
        if exc.code == "season_metadata_unavailable":
            status = 502
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.get(
    "/libraries/{library_id}/media/tv/{tmdb_id}/seasons/{season_number}/completeness",
    response_model=EpisodeCompletenessResponse,
)
async def get_episode_completeness(
    library_id: str,
    tmdb_id: int,
    season_number: int,
    request: Request,
    service: SeasonServiceDependency,
    language: str = Query(default="zh-CN", min_length=2, max_length=32),
    fallback_language: str = Query(default="en-US", min_length=2, max_length=32),
    refresh: bool = Query(default=False),
) -> EpisodeCompletenessResponse:
    library, run, entries, identities, snapshot_current = await _load_library_scope(
        request, library_id
    )
    try:
        season = await service.get(
            tmdb_id,
            season_number,
            language=language,
            fallback_language=fallback_language,
            refresh=refresh,
        )
    except SeasonMetadataError as exc:
        status = 404 if exc.code in {"season_not_found", "library_not_found"} else 422
        if exc.code == "season_metadata_unavailable":
            status = 502
        raise HTTPException(status_code=status, detail=exc.code) from exc

    snapshot = _inventory_snapshot(
        run, entries, identities, snapshot_current=snapshot_current
    )
    # Stale, unknown, and incomplete scans all remain non-conclusive.  This
    # prevents a delayed or partial remote listing from becoming a "missing"
    # episode conclusion.
    inventory_complete = snapshot.freshness.status.value == "fresh"
    baseline = tuple(
        EpisodeBaseline(
            episode_number=episode.episode_number,
            aired=_episode_aired(episode.air_date),
            special=season_number == 0,
        )
        for episode in season.episodes
        if episode.episode_number > 0
    )
    matrix = build_episode_matrix(
        baseline,
        _episode_file_references(
            entries,
            identities,
            tmdb_id=tmdb_id,
            season_number=season_number,
        ),
        inventory_complete=inventory_complete,
    )
    return EpisodeCompletenessResponse(
        library_id=library.id,
        series_tmdb_id=tmdb_id,
        season_number=season_number,
        season=season,
        freshness=_freshness_response(snapshot),
        inventory_complete=matrix.inventory_complete,
        conclusion_available=matrix.conclusion_available,
        items=[
            EpisodeCompletenessItemResponse(
                episode_number=item.episode_number,
                status=item.status.value,
                file_ids=list(item.file_ids),
            )
            for item in matrix.items
        ],
        unrecognized_file_ids=list(matrix.unrecognized_file_ids),
        missing_episodes=list(matrix.missing_episodes),
        duplicate_episodes=list(matrix.duplicate_episodes),
    )


async def _load_library_scope(request: Request, library_id: str):
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        run = await session.scalar(
            select(LibraryScanRun)
            .where(LibraryScanRun.library_id == library_id)
            .order_by(LibraryScanRun.created_at.desc(), LibraryScanRun.id.desc())
            .limit(1)
        )
        if run is None:
            return library, None, [], {}, False
        snapshot_current = bool(
            library.enabled
            and library.scope_verified
            and run.root_directory_id == library.root_directory_id
            and run.complete
            and run.state == "completed"
            and run.snapshot_revision is not None
            and await source_snapshot_is_current(
                session,
                library_id=library_id,
                source_scan_run_id=run.id,
                source_snapshot_revision=run.snapshot_revision,
            )
        )
        all_entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry)
                    .where(
                        LibraryScanEntry.scan_run_id == run.id
                    )
                    .order_by(LibraryScanEntry.object_id)
                )
            ).all()
        )
        if snapshot_current:
            checkpoint = await session.get(LibraryScanCheckpoint, run.id)
            try:
                validate_complete_scan_evidence(
                    run,
                    checkpoint,
                    all_entries,
                    root_directory_id=library.root_directory_id,
                    require_tree=True,
                )
            except LibraryIndexError:
                snapshot_current = False
        entries = [entry for entry in all_entries if not entry.is_directory]
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
    return library, run, entries, identities, snapshot_current


def _inventory_snapshot(run, entries, identities, *, snapshot_current: bool):
    if run is None:
        return build_snapshot((), complete=False, captured_at=None)
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
                tmdb_id=(
                    identities[entry.object_id].tmdb_id
                    if entry.object_id in identities
                    else None
                ),
                media_type=(
                    identities[entry.object_id].media_type
                    if entry.object_id in identities
                    else None
                ),
                season=(
                    identities[entry.object_id].season
                    if entry.object_id in identities
                    else None
                ),
                episode_start=(
                    identities[entry.object_id].episode_start
                    if entry.object_id in identities
                    else None
                ),
                episode_end=(
                    identities[entry.object_id].episode_end
                    if entry.object_id in identities
                    else None
                ),
            )
            for entry in entries
        ),
        complete=bool(
            snapshot_current and run.complete and run.state == "completed"
        ),
        captured_at=captured_at,
    )


def _episode_file_references(
    entries,
    identities,
    *,
    tmdb_id: int,
    season_number: int,
) -> tuple[EpisodeFileReference, ...]:
    references: list[EpisodeFileReference] = []
    for entry in entries:
        identity = identities.get(entry.object_id)
        if (
            identity is not None
            and identity.tmdb_id == tmdb_id
            and identity.media_type == "tv"
            and identity.season == season_number
        ):
            numbers = _episode_range(identity.episode_start, identity.episode_end)
            references.append(
                EpisodeFileReference(
                    entry.object_id,
                    numbers,
                    special=season_number == 0,
                    recognized=bool(numbers),
                )
            )
            continue

        # An unconfirmed filename can be surfaced for manual mapping, but it
        # must never count as owned by the requested TMDB series.
        parsed = parse_media_filename(entry.name)
        if parsed.season != season_number or parsed.episode_start is None:
            continue
        numbers = _episode_range(parsed.episode_start, parsed.episode_end)
        references.append(
            EpisodeFileReference(
                entry.object_id,
                numbers,
                special=season_number == 0,
                recognized=False,
            )
        )
    return tuple(references)


def _episode_range(start: int | None, end: int | None) -> tuple[int, ...]:
    if start is None:
        return ()
    end = end or start
    if end < start or end - start > 200:
        return ()
    return tuple(range(start, end + 1))


def _episode_aired(value: str | None) -> bool:
    if not value:
        return False
    try:
        aired = datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return False
    return aired <= datetime.now(UTC).date()


def _freshness_response(snapshot):
    freshness = snapshot.freshness
    from watch_assistant.schemas import InventoryFreshnessResponse

    return InventoryFreshnessResponse(
        complete=freshness.complete,
        captured_at=freshness.captured_at,
        age_seconds=freshness.age_seconds,
        threshold_seconds=freshness.threshold_seconds,
        status=freshness.status.value,
    )
