"""Search orchestration, resource persistence, and cache handling."""

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.pansou import (
    LinkCheckItem,
    LinkCheckState,
    PanSouClient,
    PanSouError,
)
from watch_assistant.adapters.prowlarr import ProwlarrClient, ProwlarrSearchResult
from watch_assistant.adapters.tmdb import TmdbClient, TmdbError, build_search_queries
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import (
    ApplicationSettings,
    MovieWatch,
    Resource,
    ResourceSearchJob,
    SearchCache,
    SourceReliability,
)
from watch_assistant.schemas import (
    HomeCatalogResponse,
    LoggingLevel,
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    ResourcePageResponse,
    ResourceSearchResponse,
    ResourceSummary,
    SearchResponse,
)
from watch_assistant.services.content_policy import (
    ContentPolicy,
    content_policy_from_json,
)
from watch_assistant.services.normalize import (
    merge_normalized_resources,
    normalize_pansou,
    normalize_prowlarr,
    normalize_source_id,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.validation import (
    resource_matches_media,
    validate_and_rank_resources,
)

FRESH_CACHE_AGE = timedelta(hours=24)
NEGATIVE_CACHE_AGE = timedelta(minutes=30)
PARTIAL_CACHE_AGE = timedelta(minutes=10)
STALE_CACHE_AGE = timedelta(days=7)
MAX_SNAPSHOT_MAGNETS = 500
LEGACY_MAGNET_LIMIT = 30


class SearchUnavailable(RuntimeError):
    pass


class ResourceSnapshotNotFound(RuntimeError):
    pass


class InvalidSeasonRequest(ValueError):
    pass


def _search_error_code_for_log(exc: BaseException) -> str:
    """Map exceptions to allowlisted log codes without rendering their text."""
    if isinstance(exc, TmdbError):
        return "tmdb_unavailable"
    if isinstance(exc, InvalidSeasonRequest):
        code = str(exc)
        return (
            code
            if code in {"season_requires_tv", "season_not_found"}
            else "invalid_season_request"
        )
    if isinstance(exc, SearchUnavailable):
        code = str(exc)
        return (
            code
            if code in {"pansou_unavailable", "resource_search_unavailable"}
            else "resource_search_unavailable"
        )
    return "resource_search_failed"


@dataclass(slots=True)
class _ResourceSearchTask:
    task_id: str
    tmdb_id: int
    media_type: MediaType
    season_number: int | None
    refresh: bool
    status: str
    created_at: datetime
    updated_at: datetime
    snapshot_revision: str | None = None
    cache_age_seconds: int | None = None
    sources: list[str] = field(default_factory=list)
    selected_season: int | None = None
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None

    def response(self) -> ResourceSearchResponse:
        return ResourceSearchResponse(
            task_id=self.task_id,
            tmdb_id=self.tmdb_id,
            media_type=self.media_type,
            season_number=self.season_number,
            status=self.status,
            snapshot_revision=self.snapshot_revision,
            cache_age_seconds=self.cache_age_seconds,
            sources=list(self.sources),
            selected_season=self.selected_season,
            warnings=list(self.warnings),
            error_code=self.error_code,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


def make_cache_key(
    tmdb_id: int,
    media_type: MediaType = MediaType.MOVIE,
    season_number: int | None = None,
) -> str:
    season = "" if season_number is None else f":season:{season_number}"
    return f"tmdb:{media_type.value}:{tmdb_id}{season}:queries:v5"


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tmdb_client: TmdbClient,
        pansou_client: PanSouClient,
        prowlarr_client: ProwlarrClient | None = None,
        crypto: SecretCrypto,
        share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
        pansou_max_concurrency: int = 6,
        prowlarr_max_concurrency: int = 4,
        pansou_request_timeout: float = 12.0,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tmdb = tmdb_client
        self._pansou = pansou_client
        self._prowlarr = prowlarr_client
        self._crypto = crypto
        self._share_domains = share_domains
        self._event_logger = event_logger
        self._pansou_limit = asyncio.Semaphore(max(1, pansou_max_concurrency))
        self._prowlarr_limit = asyncio.Semaphore(max(1, prowlarr_max_concurrency))
        self._prowlarr_state_lock = asyncio.Lock()
        self._prowlarr_usage: dict[int, int] = {}
        self._prowlarr_idle: dict[int, asyncio.Event] = {}
        self._pansou_timeout = pansou_request_timeout
        self._search_locks: dict[tuple[MediaType, int], asyncio.Lock] = {}
        self._resource_search_tasks: dict[
            tuple[MediaType, int, int | None], _ResourceSearchTask
        ] = {}
        self._resource_search_finalize_locks: dict[str, asyncio.Lock] = {}

    async def get_movie(self, tmdb_id: int) -> MovieMetadata:
        return await self._tmdb.get_movie(tmdb_id)

    async def get_media(self, tmdb_id: int, media_type: MediaType) -> MovieMetadata:
        return await self._tmdb.get_media(tmdb_id, media_type)

    async def get_popular(self) -> list[MovieMetadata]:
        return _visible_media(
            await self._tmdb.get_popular(), await self._load_content_policy()
        )

    async def get_home_catalog(self) -> HomeCatalogResponse:
        policy = await self._load_content_policy()
        (
            popular,
            now_playing,
            upcoming,
            top_rated,
            tv_popular,
            tv_on_the_air,
            tv_top_rated,
        ) = await asyncio.gather(
            self._tmdb.get_feed("popular"),
            self._tmdb.get_feed("now_playing"),
            self._tmdb.get_feed("upcoming"),
            self._tmdb.get_feed("top_rated"),
            self._tmdb.get_feed("popular", MediaType.TV),
            self._tmdb.get_feed("on_the_air", MediaType.TV),
            self._tmdb.get_feed("top_rated", MediaType.TV),
        )
        return HomeCatalogResponse(
            popular=_visible_media(popular, policy),
            now_playing=_visible_media(now_playing, policy),
            upcoming=_visible_media(upcoming, policy),
            top_rated=_visible_media(top_rated, policy),
            tv_popular=_visible_media(tv_popular, policy),
            tv_on_the_air=_visible_media(tv_on_the_air, policy),
            tv_top_rated=_visible_media(tv_top_rated, policy),
        )

    async def discover_media(
        self,
        *,
        media_type: MediaType,
        genre_id: int | None,
        year: int | None,
        sort: str,
        page: int,
    ) -> MovieCollectionResponse:
        response = await self._tmdb.discover_media(
            media_type=media_type,
            genre_id=genre_id,
            year=year,
            sort=sort,
            page=page,
        )
        return _filter_media_collection(response, await self._load_content_policy())

    async def get_popular_page(self, page: int) -> MovieCollectionResponse:
        response = await self._tmdb.get_feed_page("popular", page=page)
        return _filter_media_collection(response, await self._load_content_policy())

    async def search_movies(self, query: str) -> list[MovieMetadata]:
        return _visible_media(
            await self._tmdb.search_movies(query), await self._load_content_policy()
        )

    async def search_media(self, query: str, page: int) -> MovieCollectionResponse:
        response = await self._tmdb.search_media(query, page=page)
        return _filter_media_collection(response, await self._load_content_policy())

    async def search(
        self,
        tmdb_id: int,
        *,
        media_type: MediaType = MediaType.MOVIE,
        refresh: bool = False,
        season_number: int | None = None,
    ) -> SearchResponse:
        started = monotonic()
        await emit_event(
            self._event_logger,
            "search.started",
            fields={
                "media_type": media_type.value,
                "season": season_number if season_number is not None else "all",
            },
        )
        try:
            response = await self._search_impl(
                tmdb_id,
                media_type=media_type,
                refresh=refresh,
                season_number=season_number,
            )
        except Exception as exc:
            await emit_event(
                self._event_logger,
                "search.failed",
                level=LoggingLevel.ERROR,
                fields={
                    "media_type": media_type.value,
                    "season": season_number if season_number is not None else "all",
                    "status": "failed",
                    "error_code": _search_error_code_for_log(exc),
                    "duration_ms": int((monotonic() - started) * 1000),
                },
            )
            raise
        await emit_event(
            self._event_logger,
            "search.completed",
            fields={
                "media_type": media_type.value,
                "season": season_number if season_number is not None else "all",
                "count": len(response.results),
                "hidden_count": response.hidden_total,
                "duration_ms": int((monotonic() - started) * 1000),
            },
        )
        return response

    async def start_resource_search(
        self,
        tmdb_id: int,
        *,
        media_type: MediaType,
        season_number: int | None,
        refresh: bool = False,
    ) -> ResourceSearchResponse:
        key = (media_type, tmdb_id, season_number)
        existing = self._resource_search_tasks.get(key)
        restored = False
        if existing is None:
            existing = await self._load_latest_resource_search_task(key)
            restored = existing is not None
        if existing is not None and existing.status in {"queued", "running"}:
            self._resource_search_tasks[key] = existing
            if restored:
                asyncio.create_task(
                    self._run_resource_search(existing), name=existing.task_id
                )
            return existing.response()
        if existing is not None and existing.status == "ready" and not refresh:
            snapshot_revision, cache_age_seconds = await self._snapshot_metadata(
                tmdb_id, media_type, season_number
            )
            if snapshot_revision is not None:
                existing.snapshot_revision = snapshot_revision
                existing.cache_age_seconds = cache_age_seconds
                async with self._resource_search_lock(existing.task_id):
                    return existing.response()

        now = datetime.now(UTC)
        snapshot_revision = None
        cache_age_seconds = None
        if not refresh:
            snapshot_revision, cache_age_seconds = await self._snapshot_metadata(
                tmdb_id, media_type, season_number
            )
        task = _ResourceSearchTask(
            task_id="resource_search_" + uuid4().hex,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season_number=season_number,
            refresh=refresh,
            status="ready" if snapshot_revision is not None else "queued",
            created_at=now,
            updated_at=now,
            snapshot_revision=snapshot_revision,
            cache_age_seconds=cache_age_seconds,
            selected_season=season_number,
        )
        self._resource_search_tasks[key] = task
        await self._save_resource_search_task(task)
        if snapshot_revision is None:
            asyncio.create_task(self._run_resource_search(task), name=task.task_id)
        return task.response()

    async def get_resource_search_task(
        self, task_id: str
    ) -> ResourceSearchResponse | None:
        task = next(
            (item for item in self._resource_search_tasks.values() if item.task_id == task_id),
            None,
        )
        if task is None:
            task = await self._load_resource_search_task(task_id)
            if task is None:
                return None
            key = (task.media_type, task.tmdb_id, task.season_number)
            self._resource_search_tasks[key] = task
            if task.status in {"queued", "running"}:
                asyncio.create_task(self._run_resource_search(task), name=task.task_id)
        if task.status in {"ready", "failed"}:
            # A worker updates the in-memory object before its final SQLite
            # commit. Serialize terminal reads with that commit so callers
            # never receive a receipt that the durable ledger cannot yet
            # reproduce after a restart.
            async with self._resource_search_lock(task.task_id):
                return task.response()
        return task.response()

    async def _run_resource_search(self, task: _ResourceSearchTask) -> None:
        task.status = "running"
        task.updated_at = datetime.now(UTC)
        await self._save_resource_search_task(task)
        try:
            response = await self.search(
                task.tmdb_id,
                media_type=task.media_type,
                refresh=task.refresh,
                season_number=task.season_number,
            )
            task.selected_season = response.selected_season or task.season_number
            task.warnings = list(response.warnings)
            task.sources = sorted({item.source for item in response.results})
            task.cache_age_seconds = response.cache_age_seconds
            task.snapshot_revision, _ = await self._snapshot_metadata(
                task.tmdb_id, task.media_type, task.season_number
            )
            task.error_code = None
            task.status = "ready"
        except InvalidSeasonRequest as exc:
            task.status = "failed"
            task.error_code = str(exc) if str(exc) in {
                "season_requires_tv", "season_not_found"
            } else "invalid_season_request"
        except TmdbError:
            task.status = "failed"
            task.error_code = "tmdb_unavailable"
        except SearchUnavailable:
            task.status = "failed"
            task.error_code = "resource_search_unavailable"
        except Exception:  # noqa: BLE001 - task state must not leak exception text
            task.status = "failed"
            task.error_code = "resource_search_failed"
        finally:
            async with self._resource_search_lock(task.task_id):
                task.updated_at = datetime.now(UTC)
                await self._save_resource_search_task(task)

    def _resource_search_lock(self, task_id: str) -> asyncio.Lock:
        lock = self._resource_search_finalize_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._resource_search_finalize_locks[task_id] = lock
        return lock

    async def _load_latest_resource_search_task(
        self, key: tuple[MediaType, int, int | None]
    ) -> _ResourceSearchTask | None:
        media_type, tmdb_id, season_number = key
        async with self._session_factory() as session:
            statement = (
                select(ResourceSearchJob)
                .where(
                    ResourceSearchJob.media_type == media_type,
                    ResourceSearchJob.tmdb_id == tmdb_id,
                    ResourceSearchJob.season_number == season_number,
                )
                .order_by(ResourceSearchJob.updated_at.desc())
                .limit(1)
            )
            row = await session.scalar(statement)
        return _resource_search_task_from_row(row) if row is not None else None

    async def _load_resource_search_task(
        self, task_id: str
    ) -> _ResourceSearchTask | None:
        async with self._session_factory() as session:
            row = await session.get(ResourceSearchJob, task_id)
        return _resource_search_task_from_row(row) if row is not None else None

    async def _save_resource_search_task(self, task: _ResourceSearchTask) -> None:
        async with self._session_factory() as session:
            row = await session.get(ResourceSearchJob, task.task_id)
            if row is None:
                row = ResourceSearchJob(task_id=task.task_id)
                session.add(row)
            row.tmdb_id = task.tmdb_id
            row.media_type = task.media_type
            row.season_number = task.season_number
            row.refresh = task.refresh
            row.status = task.status
            row.snapshot_revision = task.snapshot_revision
            row.query_plan_version = "v5"
            row.cache_age_seconds = task.cache_age_seconds
            row.sources_json = json.dumps(task.sources, ensure_ascii=False)
            row.selected_season = task.selected_season
            row.warnings_json = json.dumps(task.warnings, ensure_ascii=False)
            row.error_code = task.error_code
            row.created_at = task.created_at
            row.updated_at = task.updated_at
            await session.commit()

    async def _snapshot_metadata(
        self, tmdb_id: int, media_type: MediaType, season_number: int | None
    ) -> tuple[str | None, int | None]:
        async with self._session_factory() as session:
            cache = await session.get(
                SearchCache, make_cache_key(tmdb_id, media_type, season_number)
            )
        if cache is None:
            return None, None
        age = max(0, int((datetime.now(UTC) - _as_utc(cache.fetched_at)).total_seconds()))
        if not _cache_is_fresh(cache, timedelta(seconds=age)):
            return None, None
        return _as_utc(cache.fetched_at).isoformat(), age

    async def _search_impl(
        self,
        tmdb_id: int,
        *,
        media_type: MediaType = MediaType.MOVIE,
        refresh: bool = False,
        season_number: int | None = None,
    ) -> SearchResponse:
        if season_number is not None and media_type != MediaType.TV:
            raise InvalidSeasonRequest("season_requires_tv")
        media = await self.get_media(tmdb_id, media_type)
        if media.media_type != MediaType.TV and season_number is not None:
            raise InvalidSeasonRequest("season_requires_tv")
        if season_number is not None and not any(
            season.season_number == season_number for season in media.seasons
        ):
            raise InvalidSeasonRequest("season_not_found")
        policy = await self._load_content_policy()
        if not policy.media_visible(media.adult):
            return SearchResponse(movie=media, results=[], hidden_total=0)
        lock_key = (media.media_type, media.tmdb_id)
        lock = self._search_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._search_locked(
                media,
                refresh=refresh,
                verify_links=refresh,
                season_number=season_number,
                policy=policy,
            )

    async def warm_media(self, media: MovieMetadata) -> bool:
        lock_key = (media.media_type, media.tmdb_id)
        lock = self._search_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            try:
                response = await self._search_locked(
                    media,
                    refresh=True,
                    verify_links=True,
                    season_number=None,
                )
            except SearchUnavailable:
                return False
        return not response.cached and "partial_upstream" not in response.warnings

    async def has_cache_since(
        self,
        tmdb_id: int,
        media_type: MediaType,
        since: datetime,
        season_number: int | None = None,
    ) -> bool:
        async with self._session_factory() as session:
            cache = await session.get(
                SearchCache, make_cache_key(tmdb_id, media_type, season_number)
            )
            return bool(cache and _as_utc(cache.fetched_at) >= _as_utc(since))

    async def list_resources(
        self,
        tmdb_id: int,
        *,
        media_type: MediaType,
        season_number: int | None,
        kind: ResourceKind | None,
        quality: str | None,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ResourcePageResponse:
        async with self._session_factory() as session:
            cache = await session.get(
                SearchCache, make_cache_key(tmdb_id, media_type, season_number)
            )
            if cache is None:
                raise ResourceSnapshotNotFound
            resources, scores = await self._load_cached_resources(session, cache)

        resources = list({resource.id: resource for resource in resources}.values())
        policy = await self._load_content_policy()
        visible_resources = [
            resource
            for resource in resources
            if policy.resource_reason(resource.name) is None
        ]
        hidden_total = len(resources) - len(visible_resources)
        hidden_reasons = _hidden_reason_counts(resources, policy)
        facets = _resource_facets(visible_resources)
        normalized_query = query.casefold().strip() if query else None
        filtered = [
            resource
            for resource in visible_resources
            if (kind is None or resource.kind == kind)
            and (quality is None or _quality_matches(resource.name, quality))
            and (
                normalized_query is None or normalized_query in resource.name.casefold()
            )
        ]
        filtered.sort(key=lambda resource: _resource_sort_key(resource, scores, sort))
        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        await emit_event(
            self._event_logger,
            "resources.page_served",
            level=LoggingLevel.DEBUG,
            fields={
                "page": page,
                "count": len(filtered[start:end]),
                "total": total,
                "hidden_count": hidden_total,
                "hidden_suspicious": hidden_reasons["suspicious"],
                "hidden_low_quality": hidden_reasons["low_quality"],
                "hidden_keyword": hidden_reasons["keyword"],
                "media_type": media_type.value,
                "season": season_number if season_number is not None else "all",
            },
        )
        return ResourcePageResponse(
            items=[
                _resource_summary(resource, scores) for resource in filtered[start:end]
            ],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=(total + page_size - 1) // page_size if total else 0,
            facets=facets,
            snapshot_revision=_as_utc(cache.fetched_at).isoformat(),
            hidden_total=hidden_total,
        )

    async def _load_content_policy(self) -> ContentPolicy:
        async with self._session_factory() as session:
            settings = await session.get(ApplicationSettings, "default")
        if settings is None:
            return ContentPolicy()
        return content_policy_from_json(settings.content_policy_json, settings.revision)

    async def list_active_watches(self) -> list[MovieMetadata]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(MovieWatch)
                .where(MovieWatch.active.is_(True))
                .order_by(MovieWatch.first_empty_at)
            )
            return [
                MovieMetadata(
                    tmdb_id=item.tmdb_id,
                    media_type=item.media_type,
                    title=item.title,
                    original_title=item.original_title,
                    release_year=item.release_year,
                )
                for item in rows
            ]

    async def _search_locked(
        self,
        media: MovieMetadata,
        *,
        refresh: bool,
        verify_links: bool,
        season_number: int | None,
        policy: ContentPolicy | None = None,
    ) -> SearchResponse:
        policy = policy or await self._load_content_policy()
        cache_key = make_cache_key(media.tmdb_id, media.media_type, season_number)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            cache = await session.get(SearchCache, cache_key)
            cache_age = _age(cache.fetched_at, now) if cache else None
            cache_usable = cache is not None and cache_age <= STALE_CACHE_AGE
            cached_resources: list[Resource] = []
            cached_scores: dict[str, dict[str, int]] = {}
            if cache_usable and cache is not None:
                cached_resources, cached_scores = await self._load_cached_resources(
                    session, cache
                )
            penalties = await self._load_source_penalties(session)
            if (
                cache is not None
                and cache_usable
                and not refresh
                and _cache_is_fresh(cache, cache_age)
            ):
                await emit_event(
                    self._event_logger,
                    "search.cache_hit",
                    level=LoggingLevel.DEBUG,
                    fields={
                        "media_type": media.media_type.value,
                        "season": season_number if season_number is not None else "all",
                        "count": len(cached_resources),
                    },
                )
                return self._response(
                    media,
                    cached_resources,
                    now,
                    cache,
                    cached=True,
                    warnings=_stored_warnings(cache),
                    selected_season=_selected_season(media, season_number),
                    score_snapshot=cached_scores,
                    policy=policy,
                )

        queries = build_search_queries(media, season_number)
        successful_pansou, successful_prowlarr, warnings, complete = (
            await self._query_sources(queries)
        )
        if not successful_pansou and not successful_prowlarr:
            if cache_usable and cache is not None:
                return self._response(
                    media,
                    cached_resources,
                    now,
                    cache,
                    cached=True,
                    warnings=_merge_warnings(warnings, ["stale_cache"]),
                    selected_season=_selected_season(media, season_number),
                    score_snapshot=cached_scores,
                    policy=policy,
                )
            raise SearchUnavailable(
                "resource_search_unavailable"
                if self._prowlarr is not None
                else "pansou_unavailable"
            )

        normalized = self._normalize_results(successful_pansou, now)
        normalized = self._merge_normalized_results(
            normalized,
            self._normalize_prowlarr_results(successful_prowlarr, now),
        )
        alternative_titles: tuple[str, ...] = ()
        candidates, rejected = validate_and_rank_resources(
            media,
            normalized,
            source_penalties=penalties,
            season_number=season_number,
        )
        if rejected:
            warnings.append("resource_mismatch_filtered")

        if complete and not candidates:
            resolved_titles = await self._alternative_titles(media)
            if resolved_titles is None:
                complete = False
            else:
                fallback_queries = _fallback_queries(
                    media, resolved_titles, season_number
                )
                alternative_titles = tuple(fallback_queries)
                if fallback_queries:
                    (
                        fallback_pansou,
                        fallback_prowlarr,
                        fallback_warnings,
                        fallback_complete,
                    ) = await self._query_sources(
                        fallback_queries, warning_offset=len(queries)
                    )
                    warnings = _merge_warnings(warnings, fallback_warnings)
                    complete = complete and fallback_complete
                    if fallback_pansou or fallback_prowlarr:
                        fallback_normalized = self._normalize_results(
                            fallback_pansou, now
                        )
                        fallback_normalized = self._merge_normalized_results(
                            fallback_normalized,
                            self._normalize_prowlarr_results(
                                fallback_prowlarr, now
                            ),
                        )
                        normalized = self._merge_normalized_results(
                            normalized,
                            fallback_normalized,
                        )
                        candidates, rejected = validate_and_rank_resources(
                            media,
                            normalized,
                            alternative_titles=alternative_titles,
                            source_penalties=penalties,
                            season_number=season_number,
                        )
                        warnings = [
                            item
                            for item in warnings
                            if item != "resource_mismatch_filtered"
                        ]
                        if rejected:
                            warnings.append("resource_mismatch_filtered")
                        if candidates:
                            warnings.append("alternative_titles_used")

        if sum(item.kind == ResourceKind.MAGNET for item in candidates) > MAX_SNAPSHOT_MAGNETS:
            warnings = _merge_warnings(warnings, ["resource_results_truncated"])
        candidates = _limit_magnet_resources(candidates, MAX_SNAPSHOT_MAGNETS)

        if not complete:
            warnings = _merge_warnings(warnings, ["partial_upstream"])
            if cache_usable and cache is not None:
                async with self._session_factory() as session:
                    await self._record_validation_outcomes(
                        session,
                        media,
                        normalized,
                        alternative_titles,
                        now,
                        season_number,
                    )
                    fresh_resources = await self._persist_resources(session, candidates, now)
                    merged_resources = _dedupe_resources(
                        [*fresh_resources, *cached_resources]
                    )
                    if (
                        sum(
                            item.kind == ResourceKind.MAGNET
                            for item in merged_resources
                        )
                        > MAX_SNAPSHOT_MAGNETS
                    ):
                        warnings = _merge_warnings(
                            warnings, ["resource_results_truncated"]
                        )
                    resources = _limit_magnet_resources(
                        merged_resources, MAX_SNAPSHOT_MAGNETS
                    )
                    score_snapshot = _resource_score_snapshot(resources)
                    for resource_id, scores in cached_scores.items():
                        score_snapshot.setdefault(resource_id, dict(scores))
                    await self._persist_cache(
                        session,
                        cache_key,
                        resources,
                        score_snapshot,
                        _merge_warnings(warnings, ["stale_cache"]),
                        now,
                        cache_kind="partial",
                    )
                    await session.commit()
                    cache = await session.get(SearchCache, cache_key)
                return self._response(
                    media,
                    resources,
                    now,
                    cache,
                    cached=True,
                    warnings=_merge_warnings(warnings, ["stale_cache"]),
                    selected_season=_selected_season(media, season_number),
                    score_snapshot=score_snapshot,
                    policy=policy,
                )
            async with self._session_factory() as session:
                await self._record_validation_outcomes(
                    session,
                    media,
                    normalized,
                    alternative_titles,
                    now,
                    season_number,
                )
                resources = await self._persist_resources(session, candidates, now)
                score_snapshot = _resource_score_snapshot(resources)
                await self._persist_cache(
                    session,
                    cache_key,
                    resources,
                    score_snapshot,
                    warnings,
                    now,
                    cache_kind="partial",
                )
                await session.commit()
            return self._response(
                media,
                resources,
                now,
                None,
                cached=False,
                warnings=warnings,
                selected_season=_selected_season(media, season_number),
                score_snapshot=score_snapshot,
                policy=policy,
            )

        preserved: list[Resource] = []
        checked_shares: list[NormalizedResource] = []
        link_states: list[LinkCheckState] = []
        if verify_links:
            (
                candidates,
                preserved,
                checked_shares,
                link_states,
                link_warnings,
            ) = await self._verify_shares(
                candidates,
                cached_resources,
            )
            warnings = _merge_warnings(warnings, link_warnings)

        async with self._session_factory() as session:
            await self._record_validation_outcomes(
                session,
                media,
                normalized,
                alternative_titles,
                now,
                season_number,
            )
            if checked_shares:
                await self._record_link_outcomes(
                    session, checked_shares, link_states, now
                )
            resources = await self._persist_resources(session, candidates, now)
            resources = _dedupe_resources([*resources, *preserved])
            found_new = False
            if season_number is None:
                found_new = await self._update_movie_watch(
                    session,
                    media,
                    has_resources=bool(resources),
                    now=now,
                )
            if not resources:
                warnings = _merge_warnings(warnings, ["watching_for_resources"])
            elif found_new:
                warnings = _merge_warnings(warnings, ["new_resources_found"])
            score_snapshot = _resource_score_snapshot(resources)
            for preserved_resource in preserved:
                preserved_scores = cached_scores.get(preserved_resource.id)
                if preserved_scores:
                    score_snapshot[preserved_resource.id] = dict(preserved_scores)
            await self._persist_cache(
                session,
                cache_key,
                resources,
                score_snapshot,
                warnings,
                now,
                cache_kind=(
                    "negative" if not resources else "positive"
                ),
            )
            await session.commit()
            cache = await session.get(SearchCache, cache_key)
            return self._response(
                media,
                resources,
                now,
                cache,
                cached=False,
                warnings=warnings,
                selected_season=_selected_season(media, season_number),
                score_snapshot=score_snapshot,
                policy=policy,
            )

    async def _query_pansou(self, query: str) -> dict:
        async with self._pansou_limit:
            return await asyncio.wait_for(
                self._pansou.search(query), timeout=self._pansou_timeout
            )

    async def _query_prowlarr(self, query: str) -> ProwlarrSearchResult:
        async with self._prowlarr_state_lock:
            client = self._prowlarr
            if client is None:
                raise RuntimeError("Prowlarr client is not configured")
            client_key = id(client)
            self._prowlarr_usage[client_key] = (
                self._prowlarr_usage.get(client_key, 0) + 1
            )
            idle = self._prowlarr_idle.setdefault(client_key, asyncio.Event())
            idle.clear()
        try:
            async with self._prowlarr_limit:
                return await client.search(query)
        finally:
            async with self._prowlarr_state_lock:
                usage = self._prowlarr_usage[client_key] - 1
                if usage:
                    self._prowlarr_usage[client_key] = usage
                else:
                    self._prowlarr_usage.pop(client_key, None)
                    self._prowlarr_idle[client_key].set()

    async def replace_prowlarr_client(
        self, client: ProwlarrClient | None
    ) -> None:
        async with self._prowlarr_state_lock:
            previous = self._prowlarr
            self._prowlarr = client
            if previous is None or previous is client:
                idle = None
            else:
                idle = self._prowlarr_idle.setdefault(id(previous), asyncio.Event())
                if not self._prowlarr_usage.get(id(previous), 0):
                    idle.set()
        if previous is not None and previous is not client and idle is not None:
            cancelled = False
            idle_task = asyncio.create_task(idle.wait())
            try:
                while not idle_task.done():
                    try:
                        await asyncio.shield(idle_task)
                    except asyncio.CancelledError:
                        cancelled = True
                await idle_task
                close = getattr(previous, "aclose", None)
                if callable(close):
                    close_task = asyncio.create_task(close())
                    while not close_task.done():
                        try:
                            await asyncio.shield(close_task)
                        except asyncio.CancelledError:
                            cancelled = True
                    await close_task
            finally:
                async with self._prowlarr_state_lock:
                    if (
                        self._prowlarr_idle.get(id(previous)) is idle
                        and not self._prowlarr_usage.get(id(previous), 0)
                    ):
                        self._prowlarr_idle.pop(id(previous), None)
            if cancelled or bool(asyncio.current_task().cancelling()):
                raise asyncio.CancelledError

    async def _query_sources(
        self, queries: tuple[str, ...], *, warning_offset: int = 0
    ) -> tuple[
        list[tuple[str, dict]],
        list[tuple[str, ProwlarrSearchResult]],
        list[str],
        bool,
    ]:
        async def safe_pansou(query: str) -> dict | BaseException:
            try:
                return await self._query_pansou(query)
            except Exception as exc:  # noqa: BLE001 - source failure is degraded
                return exc

        if self._prowlarr is None:
            pansou_results = await asyncio.gather(
                *(safe_pansou(query) for query in queries)
            )
            successful = [
                (query, result)
                for query, result in zip(queries, pansou_results, strict=True)
                if isinstance(result, dict)
            ]
            warnings = [
                f"pansou_query_failed:{warning_offset + index + 1}"
                for index, result in enumerate(pansou_results)
                if isinstance(result, BaseException)
            ]
            failed_count = len(warnings)
            if failed_count:
                await emit_event(
                    getattr(self, "_event_logger", None),
                    "search.source_degraded",
                    level=LoggingLevel.WARNING,
                    fields={
                        "source": "PanSou",
                        "status": "degraded",
                        "count": failed_count,
                        "total": len(queries),
                    },
                )
            return successful, [], warnings, not warnings

        async def safe_prowlarr(query: str) -> ProwlarrSearchResult | BaseException:
            try:
                return await self._query_prowlarr(query)
            except Exception as exc:  # noqa: BLE001 - source failure is degraded
                return exc

        query_results = await asyncio.gather(
            *(
                asyncio.gather(safe_pansou(query), safe_prowlarr(query))
                for query in queries
            )
        )
        successful_pansou: list[tuple[str, dict]] = []
        successful_prowlarr: list[tuple[str, ProwlarrSearchResult]] = []
        warnings: list[str] = []
        upstream_partial = False
        failed_counts = {"pansou": 0, "prowlarr": 0}
        for index, (pansou_result, prowlarr_result) in enumerate(
            query_results, start=1
        ):
            if isinstance(pansou_result, dict):
                successful_pansou.append((queries[index - 1], pansou_result))
            else:
                failed_counts["pansou"] += 1
                warnings.append(
                    f"pansou_query_failed:{warning_offset + index}"
                )
            if isinstance(prowlarr_result, ProwlarrSearchResult):
                successful_prowlarr.append((queries[index - 1], prowlarr_result))
                if prowlarr_result.unsupported_count:
                    warnings.append("prowlarr_unsupported_results")
                if prowlarr_result.truncated:
                    upstream_partial = True
                    warnings.append("prowlarr_results_truncated")
                    warnings.append("partial_upstream")
            else:
                failed_counts["prowlarr"] += 1
                warnings.append(
                    f"prowlarr_query_failed:{warning_offset + index}"
                )
        for source, count in failed_counts.items():
            if count:
                await emit_event(
                    getattr(self, "_event_logger", None),
                    "search.source_degraded",
                    level=LoggingLevel.WARNING,
                    fields={
                        "source": {
                            "pansou": "PanSou",
                            "prowlarr": "Prowlarr",
                        }[source],
                        "status": "degraded",
                        "count": count,
                        "total": len(queries),
                    },
                )
        return (
            successful_pansou,
            successful_prowlarr,
            _merge_warnings(warnings, []),
            not any("_query_failed:" in warning for warning in warnings)
            and not upstream_partial,
        )

    def _normalize_results(
        self,
        results: list[tuple[str, dict]],
        now: datetime,
    ) -> list[NormalizedResource]:
        normalized_by_key: dict[str, NormalizedResource] = {}
        for query, result in results:
            for resource in normalize_pansou(
                result,
                share_domains=self._share_domains,
                captured_at=now,
            ):
                resource.metadata["search_queries"] = [query]
                resource.metadata["sources"] = [resource.source]
                resource.metadata["source_observations"] = [
                    _source_observation(resource)
                ]
                existing = normalized_by_key.get(resource.canonical_key)
                normalized_by_key[resource.canonical_key] = (
                    resource
                    if existing is None
                    else _merge_normalized(existing, resource)
                )
        return list(normalized_by_key.values())

    def _normalize_prowlarr_results(
        self,
        results: list[tuple[str, ProwlarrSearchResult]],
        now: datetime,
    ) -> list[NormalizedResource]:
        normalized_by_key: dict[str, NormalizedResource] = {}
        for _query, result in results:
            for resource in normalize_prowlarr(result.releases, captured_at=now):
                resource.metadata["sources"] = [resource.source]
                resource.metadata["source_observations"] = [
                    _source_observation(resource)
                ]
                existing = normalized_by_key.get(resource.canonical_key)
                normalized_by_key[resource.canonical_key] = (
                    resource
                    if existing is None
                    else _merge_normalized(existing, resource)
                )
        return list(normalized_by_key.values())

    @staticmethod
    def _merge_normalized_results(
        current: list[NormalizedResource],
        additions: list[NormalizedResource],
    ) -> list[NormalizedResource]:
        merged = {item.canonical_key: item for item in current}
        for addition in additions:
            existing = merged.get(addition.canonical_key)
            merged[addition.canonical_key] = (
                addition if existing is None else _merge_normalized(existing, addition)
            )
        return list(merged.values())

    async def _alternative_titles(self, media: MovieMetadata) -> tuple[str, ...] | None:
        try:
            titles = await self._tmdb.get_alternative_titles(
                media.tmdb_id, media.media_type
            )
        except TmdbError:
            return None
        excluded = {
            item.casefold() for item in (media.title, media.original_title) if item
        }
        return tuple(title for title in titles if title.casefold() not in excluded)

    async def _verify_shares(
        self,
        candidates: list[NormalizedResource],
        cached_resources: list[Resource],
    ) -> tuple[
        list[NormalizedResource],
        list[Resource],
        list[NormalizedResource],
        list[LinkCheckState],
        list[str],
    ]:
        shares = [item for item in candidates if item.kind == ResourceKind.SHARE]
        if not shares:
            return candidates, [], [], [], []
        cached_shares = {
            item.canonical_key: item
            for item in cached_resources
            if item.kind == ResourceKind.SHARE
        }
        try:
            states = await self._pansou.check_links(
                [LinkCheckItem(item.url, item.password) for item in shares]
            )
        except PanSouError:
            magnets = [item for item in candidates if item.kind == ResourceKind.MAGNET]
            return (
                magnets,
                list(cached_shares.values()),
                [],
                [],
                ["link_check_inconclusive"],
            )

        states_by_key = {
            item.canonical_key: state
            for item, state in zip(shares, states, strict=True)
        }
        accepted: list[NormalizedResource] = []
        preserved: list[Resource] = []
        inconclusive = False
        for item in candidates:
            if item.kind == ResourceKind.MAGNET:
                accepted.append(item)
                continue
            state = states_by_key[item.canonical_key]
            if state == LinkCheckState.OK:
                accepted.append(item)
            elif state in {LinkCheckState.UNCERTAIN, LinkCheckState.UNSUPPORTED}:
                inconclusive = True
                cached = cached_shares.get(item.canonical_key)
                if cached is not None:
                    preserved.append(cached)
        warnings = ["link_check_inconclusive"] if inconclusive else []
        return accepted, preserved, shares, states, warnings

    async def _load_source_penalties(self, session: AsyncSession) -> dict[str, int]:
        rows = await session.scalars(select(SourceReliability))
        return {item.source: source_penalty(item) for item in rows}

    async def _record_validation_outcomes(
        self,
        session: AsyncSession,
        media: MovieMetadata,
        resources: list[NormalizedResource],
        alternative_titles: tuple[str, ...],
        now: datetime,
        season_number: int | None,
    ) -> None:
        counts: dict[str, list[int]] = {}
        for resource in resources:
            values = counts.setdefault(resource.source, [0, 0])
            if resource_matches_media(
                media,
                resource.name,
                alternative_titles=alternative_titles,
                season_number=season_number,
            ):
                values[0] += 1
            else:
                values[1] += 1
        for source, (accepted, rejected) in counts.items():
            stmt = insert(SourceReliability).values(
                source=source,
                accepted_count=accepted,
                rejected_count=rejected,
                link_ok_count=0,
                link_bad_count=0,
                updated_at=now,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SourceReliability.source],
                    set_={
                        "accepted_count": SourceReliability.accepted_count
                        + stmt.excluded.accepted_count,
                        "rejected_count": SourceReliability.rejected_count
                        + stmt.excluded.rejected_count,
                        "updated_at": now,
                    },
                )
            )

    async def _record_link_outcomes(
        self,
        session: AsyncSession,
        resources: list[NormalizedResource],
        states: list[LinkCheckState],
        now: datetime,
    ) -> None:
        counts: dict[str, list[int]] = {}
        for resource, state in zip(resources, states, strict=True):
            values = counts.setdefault(resource.source, [0, 0])
            if state == LinkCheckState.OK:
                values[0] += 1
            elif state in {LinkCheckState.BAD, LinkCheckState.LOCKED}:
                values[1] += 1
        for source, (link_ok, link_bad) in counts.items():
            if not link_ok and not link_bad:
                continue
            stmt = insert(SourceReliability).values(
                source=source,
                accepted_count=0,
                rejected_count=0,
                link_ok_count=link_ok,
                link_bad_count=link_bad,
                updated_at=now,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SourceReliability.source],
                    set_={
                        "link_ok_count": SourceReliability.link_ok_count
                        + stmt.excluded.link_ok_count,
                        "link_bad_count": SourceReliability.link_bad_count
                        + stmt.excluded.link_bad_count,
                        "updated_at": now,
                    },
                )
            )

    async def _update_movie_watch(
        self,
        session: AsyncSession,
        media: MovieMetadata,
        *,
        has_resources: bool,
        now: datetime,
    ) -> bool:
        watch = await session.get(MovieWatch, (media.media_type, media.tmdb_id))
        if has_resources:
            if watch is None or not watch.active:
                return False
            watch.active = False
            watch.last_checked_at = now
            watch.found_at = now
            return True
        if watch is None:
            session.add(
                MovieWatch(
                    media_type=media.media_type,
                    tmdb_id=media.tmdb_id,
                    title=media.title,
                    original_title=media.original_title,
                    release_year=media.release_year,
                    active=True,
                    first_empty_at=now,
                    last_checked_at=now,
                )
            )
            return False
        watch.title = media.title
        watch.original_title = media.original_title
        watch.release_year = media.release_year
        watch.active = True
        watch.last_checked_at = now
        watch.found_at = None
        return False

    async def _load_cached_resources(
        self, session: AsyncSession, cache: SearchCache
    ) -> tuple[list[Resource], dict[str, dict[str, int]]]:
        resource_ids, score_snapshot = _decode_resource_snapshot(
            cache.resource_ids_json
        )
        if not resource_ids:
            return [], score_snapshot
        rows = await session.scalars(
            select(Resource).where(Resource.id.in_(resource_ids))
        )
        by_id = {resource.id: resource for resource in rows}
        resources = [
            by_id[resource_id] for resource_id in resource_ids if resource_id in by_id
        ]
        return resources, _complete_score_snapshot(resources, score_snapshot)

    async def _persist_resources(self, session, resources, now):
        if not resources:
            return []
        existing_rows = await session.scalars(
            select(Resource).where(
                Resource.canonical_key.in_([item.canonical_key for item in resources])
            )
        )
        existing_by_key = {item.canonical_key: item for item in existing_rows}
        expires_at = now + STALE_CACHE_AGE
        values = []
        for resource in resources:
            existing = existing_by_key.get(resource.canonical_key)
            size_bytes = resource.size_bytes
            if size_bytes is None and existing is not None:
                size_bytes = existing.size_bytes
            seeders = resource.seeders
            if seeders is None and existing is not None:
                seeders = existing.seeders
            resource_id = (
                "res_" + sha256(resource.canonical_key.encode()).hexdigest()[:24]
            )
            values.append(
                {
                    "id": resource_id,
                    "kind": resource.kind,
                    "canonical_key": resource.canonical_key,
                    "encrypted_url": self._crypto.encrypt(resource.url),
                    "encrypted_password": self._crypto.encrypt(resource.password)
                    if resource.password
                    else existing.encrypted_password
                    if existing is not None
                    else None,
                    "name": resource.name,
                    "size_bytes": size_bytes,
                    "seeders": seeders,
                    "source": normalize_source_id(resource.source),
                    "captured_at": resource.captured_at,
                    "expires_at": expires_at,
                    "metadata_json": json.dumps(
                        _merge_persisted_resource_metadata(existing, resource),
                        ensure_ascii=False,
                    ),
                }
            )
        stmt = insert(Resource).values(values)
        update_columns = {
            key: getattr(stmt.excluded, key)
            for key in values[0]
            if key not in {"id", "canonical_key"}
        }
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Resource.canonical_key], set_=update_columns
            )
        )
        canonical_keys = [item["canonical_key"] for item in values]
        rows = await session.scalars(
            select(Resource)
            .where(Resource.canonical_key.in_(canonical_keys))
            .execution_options(populate_existing=True)
        )
        by_key = {resource.canonical_key: resource for resource in rows}
        return [by_key[key] for key in canonical_keys]

    async def _persist_cache(
        self,
        session,
        cache_key,
        resources,
        score_snapshot,
        warnings,
        now,
        *,
        cache_kind: str = "positive",
    ):
        snapshot = {
            "version": 1,
            "resources": [
                {
                    "resource_id": item.id,
                    **score_snapshot.get(item.id, {}),
                }
                for item in resources
            ],
        }
        values = {
            "cache_key": cache_key,
            "resource_ids_json": json.dumps(snapshot),
            "warnings_json": json.dumps(warnings),
            "cache_kind": cache_kind,
            "fetched_at": now,
            "expires_at": now + _cache_ttl(cache_kind),
        }
        stmt = insert(SearchCache).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[SearchCache.cache_key],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in values
                    if key != "cache_key"
                },
            )
        )

    @staticmethod
    def _response(
        movie: MovieMetadata,
        resources: list[Resource],
        now: datetime,
        cache: SearchCache | None,
        *,
        cached: bool,
        warnings: list[str] | None = None,
        selected_season: int | None = None,
        score_snapshot: Mapping[str, Mapping[str, int]] | None = None,
        policy: ContentPolicy,
    ) -> SearchResponse:
        visible = [
            resource
            for resource in resources
            if policy.resource_reason(resource.name) is None
        ]
        hidden_total = len(resources) - len(visible)
        response_scores = _complete_score_snapshot(visible, score_snapshot or {})
        ordered_resources = sorted(
            visible,
            key=lambda resource: _resource_sort_key(
                resource, response_scores, "comprehensive"
            ),
        )
        response_warnings = list(warnings or [])
        if sum(item.kind == ResourceKind.MAGNET for item in ordered_resources) > LEGACY_MAGNET_LIMIT:
            response_warnings = _merge_warnings(
                response_warnings, ["resource_results_truncated"]
            )
        visible_resources = _limit_magnet_resources(
            ordered_resources, LEGACY_MAGNET_LIMIT
        )
        return SearchResponse(
            movie=movie,
            results=[
                _resource_summary(item, response_scores) for item in visible_resources
            ],
            warnings=response_warnings,
            cached=cached,
            cache_age_seconds=(
                max(0, int(_age(cache.fetched_at, now).total_seconds()))
                if cache
                else None
            ),
            selected_season=selected_season,
            hidden_total=hidden_total,
        )


def _resource_search_task_from_row(row: ResourceSearchJob) -> _ResourceSearchTask:
    try:
        sources = json.loads(row.sources_json)
    except (TypeError, json.JSONDecodeError):
        sources = []
    try:
        warnings = json.loads(row.warnings_json)
    except (TypeError, json.JSONDecodeError):
        warnings = []
    return _ResourceSearchTask(
        task_id=row.task_id,
        tmdb_id=row.tmdb_id,
        media_type=MediaType(row.media_type),
        season_number=row.season_number,
        refresh=bool(row.refresh),
        status=row.status,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        snapshot_revision=row.snapshot_revision,
        cache_age_seconds=row.cache_age_seconds,
        sources=[item for item in sources if isinstance(item, str)]
        if isinstance(sources, list)
        else [],
        selected_season=row.selected_season,
        warnings=[item for item in warnings if isinstance(item, str)]
        if isinstance(warnings, list)
        else [],
        error_code=row.error_code,
    )


def _merge_normalized(
    existing: NormalizedResource,
    candidate: NormalizedResource,
) -> NormalizedResource:
    has_search_queries = (
        "search_queries" in existing.metadata
        or "search_queries" in candidate.metadata
    )
    queries = list(existing.metadata.get("search_queries", []))
    for query in candidate.metadata.get("search_queries", []):
        if query not in queries:
            queries.append(query)
    sources = list(existing.metadata.get("sources", []))
    for source in candidate.metadata.get("sources", []):
        if source not in sources:
            sources.append(source)
    observations = _merge_source_observations(
        existing.metadata.get("source_observations"),
        candidate.metadata.get("source_observations"),
    )
    merged = merge_normalized_resources(existing, candidate)
    if has_search_queries:
        merged.metadata["search_queries"] = queries
    else:
        merged.metadata.pop("search_queries", None)
    merged.metadata["sources"] = sources
    merged.metadata["source_observations"] = observations
    return merged


def _source_observation(resource: NormalizedResource) -> dict[str, str]:
    """Keep provenance to safe source metadata, never request details."""
    return {
        "source": normalize_source_id(resource.source),
        "captured_at": _as_utc(resource.captured_at).isoformat(),
    }


def _merge_source_observations(*values: object) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            source = item.get("source")
            captured_at = item.get("captured_at")
            if not isinstance(source, str) or not isinstance(captured_at, str):
                continue
            normalized = normalize_source_id(source)
            observation = (normalized, captured_at[:64])
            if observation in seen:
                continue
            seen.add(observation)
            merged.append({"source": normalized, "captured_at": observation[1]})
    return merged


def _stored_warnings(cache: SearchCache) -> list[str]:
    try:
        warnings = json.loads(cache.warnings_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(warnings, list):
        return []
    return [item for item in warnings if isinstance(item, str)]


def _cache_ttl(cache_kind: str) -> timedelta:
    if cache_kind == "negative":
        return NEGATIVE_CACHE_AGE
    if cache_kind == "partial":
        return PARTIAL_CACHE_AGE
    return FRESH_CACHE_AGE


def _cache_is_fresh(cache: SearchCache, age: timedelta) -> bool:
    # The age rule keeps legacy rows compatible; expires_at remains a cleanup hint.
    return age <= _cache_ttl(getattr(cache, "cache_kind", "positive"))


def _resource_summary(
    item: Resource,
    score_snapshot: Mapping[str, Mapping[str, int]] | None,
) -> ResourceSummary:
    metadata = _resource_metadata(item)
    size_source = metadata.get("size_source")
    seeders_source = metadata.get("seeders_source")
    observed_at = metadata.get("seeders_observed_at")
    normalized_source = normalize_source_id(item.source)
    sources: list[str] = []
    raw_sources = metadata.get("sources")
    if isinstance(raw_sources, list):
        for source in raw_sources:
            normalized = normalize_source_id(source)
            if normalized not in sources:
                sources.append(normalized)
    if normalized_source not in sources:
        sources.append(normalized_source)
    return ResourceSummary(
        resource_id=item.id,
        kind=item.kind,
        name=item.name,
        size_bytes=item.size_bytes,
        seeders=item.seeders,
        source=normalized_source,
        sources=sources,
        source_count=len(sources),
        captured_at=_as_utc(item.captured_at),
        size_source=(size_source if size_source in {"pansou", "inspection"} else None),
        seeders_source=seeders_source if seeders_source == "pansou" else None,
        seeders_observed_at=(
            _parse_metadata_datetime(observed_at)
            if seeders_source == "pansou"
            else None
        ),
        rank_score=_snapshot_score(score_snapshot, item.id, "rank_score"),
        relevance_score=_snapshot_score(score_snapshot, item.id, "relevance_score"),
        completeness_score=_snapshot_score(
            score_snapshot, item.id, "completeness_score"
        ),
    )


def _resource_metadata(resource: Resource) -> dict:
    try:
        metadata = json.loads(resource.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _merge_persisted_resource_metadata(
    existing: Resource | None,
    current: NormalizedResource,
) -> dict[str, object]:
    """Keep source history and optional evidence across partial refreshes."""
    existing_metadata = _resource_metadata(existing) if existing is not None else {}
    current_metadata = current.metadata
    merged = {**existing_metadata, **current_metadata}

    source_values: list[object] = [current_metadata.get("sources"), current.source]
    observation_values: list[object] = [
        current_metadata.get("source_observations"),
        [
            {
                "source": current.source,
                "captured_at": _as_utc(current.captured_at).isoformat(),
            }
        ],
    ]
    if existing is not None:
        source_values = [
            existing_metadata.get("sources"),
            existing.source,
            *source_values,
        ]
        observation_values = [
            existing_metadata.get("source_observations"),
            [
                {
                    "source": existing.source,
                    "captured_at": _as_utc(existing.captured_at).isoformat(),
                }
            ],
            *observation_values,
        ]
    merged["sources"] = _merge_safe_source_ids(*source_values)
    merged["source_observations"] = _merge_source_observations(
        *observation_values
    )

    search_queries = _merge_metadata_strings(
        existing_metadata.get("search_queries"),
        current_metadata.get("search_queries"),
    )
    if search_queries:
        merged["search_queries"] = search_queries
    else:
        merged.pop("search_queries", None)
    return merged


def _merge_safe_source_ids(*values: object) -> list[str]:
    sources: list[str] = []
    for value in values:
        candidates = (
            [value]
            if isinstance(value, str)
            else value
            if isinstance(value, list)
            else []
        )
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = normalize_source_id(candidate)
            if normalized not in sources:
                sources.append(normalized)
    return sources


def _merge_metadata_strings(*values: object) -> list[str]:
    merged: list[str] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item not in merged:
                merged.append(item)
    return merged


def _parse_metadata_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return _as_utc(parsed)


def _merge_warnings(current: list[str], additions: list[str]) -> list[str]:
    return list(dict.fromkeys([*current, *additions]))


def _dedupe_resources(resources: list[Resource]) -> list[Resource]:
    deduplicated: dict[str, Resource] = {}
    for resource in resources:
        deduplicated.setdefault(resource.canonical_key, resource)
    return list(deduplicated.values())


def _fallback_queries(
    media: MovieMetadata,
    alternative_titles: tuple[str, ...],
    season_number: int | None = None,
) -> list[str]:
    existing = {item.casefold() for item in build_search_queries(media, season_number)}
    queries: list[str] = []
    for title in alternative_titles:
        normalized = " ".join(title.split())
        if not normalized or normalized.casefold() in existing:
            continue
        existing.add(normalized.casefold())
        queries.append(normalized)
        if len(queries) == 2:
            break
    return queries


def _limit_magnet_resources(
    resources: list[NormalizedResource], limit: int = LEGACY_MAGNET_LIMIT
) -> list[NormalizedResource]:
    magnets = [item for item in resources if item.kind == ResourceKind.MAGNET]
    allowed = {item.canonical_key for item in magnets[:limit]}
    return [
        item
        for item in resources
        if item.kind == ResourceKind.SHARE or item.canonical_key in allowed
    ]


def _resource_facets(resources: list[Resource]) -> dict[str, int]:
    facets = {
        "magnet": 0,
        "share": 0,
        "4k": 0,
        "1080p": 0,
        "720p": 0,
        "subtitle": 0,
    }
    for resource in resources:
        if resource.kind == ResourceKind.MAGNET:
            facets["magnet"] += 1
        elif resource.kind == ResourceKind.SHARE:
            facets["share"] += 1
        for quality in _quality_tags(resource.name):
            facets[quality] += 1
    return facets


_QUALITY_PATTERNS = {
    "4k": re.compile(r"(?<![a-z0-9])(?:4k|2160p|uhd)(?![a-z0-9])"),
    "1080p": re.compile(r"(?<![a-z0-9])1080p(?![a-z0-9])"),
    "720p": re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])"),
    "subtitle": re.compile(
        r"(?<![a-z0-9])(?:sub|subtitles?|chs|cht)(?![a-z0-9])"
        r"|字幕|简中|繁中|双语"
    ),
}


def _quality_tags(name: str) -> frozenset[str]:
    value = name.casefold()
    return frozenset(
        quality
        for quality, pattern in _QUALITY_PATTERNS.items()
        if pattern.search(value) is not None
    )


def _quality_matches(name: str, quality: str) -> bool:
    return quality in _quality_tags(name)


def _visible_media(
    items: list[MovieMetadata], policy: ContentPolicy
) -> list[MovieMetadata]:
    return [item for item in items if policy.media_visible(item.adult)]


def _hidden_reason_counts(
    resources: list[Resource], policy: ContentPolicy
) -> dict[str, int]:
    counts = {"suspicious": 0, "low_quality": 0, "keyword": 0}
    for resource in resources:
        reason = policy.resource_reason(resource.name)
        if reason in counts:
            counts[reason] += 1
    return counts


def _filter_media_collection(
    response: MovieCollectionResponse, policy: ContentPolicy
) -> MovieCollectionResponse:
    items = _visible_media(response.results, policy)
    # TMDB only provides page-local results here; preserve its collection-wide
    # pagination metadata instead of presenting a fabricated global count.
    return response.model_copy(
        update={
            "results": items,
        }
    )


def _resource_sort_key(
    resource: Resource,
    scores: Mapping[str, Mapping[str, int]],
    sort: str,
) -> tuple[object, ...]:
    resource_scores = scores.get(resource.id, {})
    comprehensive = _comprehensive_sort_key(resource, resource_scores)
    if sort == "comprehensive":
        return comprehensive
    if sort == "relevance":
        return (-resource_scores.get("relevance_score", 0), *comprehensive)
    if sort == "completeness":
        return (-resource_scores.get("completeness_score", 0), *comprehensive)
    if sort == "size":
        return (*_optional_descending(resource.size_bytes), *comprehensive)
    if sort == "seeders":
        return (*_optional_descending(resource.seeders), *comprehensive)
    return comprehensive


def _comprehensive_sort_key(
    resource: Resource,
    scores: Mapping[str, int],
) -> tuple[object, ...]:
    return (
        -scores.get("rank_score", 0),
        *_optional_descending(resource.seeders),
        *_optional_descending(resource.size_bytes),
        -_as_utc(resource.captured_at).timestamp(),
        resource.id,
    )


def _optional_descending(value: int | None) -> tuple[int, int]:
    if value is None or value < 0:
        return (1, 0)
    return (0, -value)


def _selected_season(media: MovieMetadata, season_number: int | None) -> int | None:
    return season_number if media.media_type == MediaType.TV else None


def _decode_resource_snapshot(
    value: str,
) -> tuple[list[str], dict[str, dict[str, int]]]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [], {}
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, str)], {}
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
        return [], {}
    resource_ids: list[str] = []
    scores: dict[str, dict[str, int]] = {}
    for item in payload["resources"]:
        if not isinstance(item, dict) or not isinstance(item.get("resource_id"), str):
            continue
        resource_id = item["resource_id"]
        resource_ids.append(resource_id)
        scores[resource_id] = {
            key: item[key]
            for key in ("rank_score", "relevance_score", "completeness_score")
            if isinstance(item.get(key), int) and 0 <= item[key] <= 100
        }
    return resource_ids, scores


def _resource_score_snapshot(
    resources: list[Resource],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for resource in resources:
        values = _resource_metadata_scores(resource)
        result[resource.id] = {
            key: values.get(key, 0)
            for key in ("rank_score", "relevance_score", "completeness_score")
        }
    return result


def _complete_score_snapshot(
    resources: list[Resource],
    snapshot: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    completed: dict[str, dict[str, int]] = {}
    for resource in resources:
        values = dict(snapshot.get(resource.id, {}))
        if len(values) < 3:
            metadata_scores = _resource_metadata_scores(resource)
            for key, value in metadata_scores.items():
                values.setdefault(key, value)
        completed[resource.id] = {
            key: values.get(key, 0)
            for key in ("rank_score", "relevance_score", "completeness_score")
        }
    return completed


def _resource_metadata_scores(resource: Resource) -> dict[str, int]:
    try:
        metadata = json.loads(resource.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    scores = {
        key: metadata[key]
        for key in ("rank_score", "relevance_score", "completeness_score")
        if isinstance(metadata.get(key), int) and 0 <= metadata[key] <= 100
    }
    return scores if len(scores) == 3 else {}


def _snapshot_score(
    snapshot: Mapping[str, Mapping[str, int]] | None,
    resource_id: str,
    key: str,
) -> int:
    value = (snapshot or {}).get(resource_id, {}).get(key, 0)
    return value if isinstance(value, int) and 0 <= value <= 100 else 0


def source_penalty(source: SourceReliability) -> int:
    total = (
        source.accepted_count
        + source.rejected_count
        + source.link_ok_count
        + source.link_bad_count
    )
    if total < 10:
        return 0
    bad = source.rejected_count + source.link_bad_count
    return min(30, round(30 * bad / total))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age(value: datetime, now: datetime) -> timedelta:
    return now - _as_utc(value)
