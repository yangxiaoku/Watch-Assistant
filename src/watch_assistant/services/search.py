"""Search orchestration, resource persistence, and cache handling."""

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.pansou import (
    LinkCheckItem,
    LinkCheckState,
    PanSouClient,
    PanSouError,
)
from watch_assistant.adapters.tmdb import TmdbClient, TmdbError, build_search_queries
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import MovieWatch, Resource, SearchCache, SourceReliability
from watch_assistant.schemas import (
    HomeCatalogResponse,
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    ResourceSummary,
    SearchResponse,
)
from watch_assistant.services.normalize import (
    merge_normalized_resources,
    normalize_pansou,
)
from watch_assistant.services.validation import (
    resource_matches_media,
    validate_and_rank_resources,
)

FRESH_CACHE_AGE = timedelta(hours=24)
STALE_CACHE_AGE = timedelta(days=7)


class SearchUnavailable(RuntimeError):
    pass


class InvalidSeasonRequest(ValueError):
    pass


def make_cache_key(
    tmdb_id: int,
    media_type: MediaType = MediaType.MOVIE,
    season_number: int | None = None,
) -> str:
    season = "" if season_number is None else f":season:{season_number}"
    return f"tmdb:{media_type.value}:{tmdb_id}{season}:queries:v4"


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tmdb_client: TmdbClient,
        pansou_client: PanSouClient,
        crypto: SecretCrypto,
        share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
        pansou_max_concurrency: int = 6,
    ) -> None:
        self._session_factory = session_factory
        self._tmdb = tmdb_client
        self._pansou = pansou_client
        self._crypto = crypto
        self._share_domains = share_domains
        self._pansou_limit = asyncio.Semaphore(max(1, pansou_max_concurrency))
        self._search_locks: dict[tuple[MediaType, int], asyncio.Lock] = {}

    async def get_movie(self, tmdb_id: int) -> MovieMetadata:
        return await self._tmdb.get_movie(tmdb_id)

    async def get_media(self, tmdb_id: int, media_type: MediaType) -> MovieMetadata:
        return await self._tmdb.get_media(tmdb_id, media_type)

    async def get_popular(self) -> list[MovieMetadata]:
        return await self._tmdb.get_popular()

    async def get_home_catalog(self) -> HomeCatalogResponse:
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
            popular=popular,
            now_playing=now_playing,
            upcoming=upcoming,
            top_rated=top_rated,
            tv_popular=tv_popular,
            tv_on_the_air=tv_on_the_air,
            tv_top_rated=tv_top_rated,
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
        return await self._tmdb.discover_media(
            media_type=media_type,
            genre_id=genre_id,
            year=year,
            sort=sort,
            page=page,
        )

    async def get_popular_page(self, page: int) -> MovieCollectionResponse:
        return await self._tmdb.get_feed_page("popular", page=page)

    async def search_movies(self, query: str) -> list[MovieMetadata]:
        return await self._tmdb.search_movies(query)

    async def search_media(self, query: str, page: int) -> MovieCollectionResponse:
        return await self._tmdb.search_media(query, page=page)

    async def search(
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
        lock_key = (media.media_type, media.tmdb_id)
        lock = self._search_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._search_locked(
                media,
                refresh=refresh,
                verify_links=refresh,
                season_number=season_number,
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
    ) -> SearchResponse:
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
                and cache_age <= FRESH_CACHE_AGE
            ):
                return self._response(
                    media,
                    cached_resources,
                    now,
                    cache,
                    cached=True,
                    warnings=_stored_warnings(cache),
                    selected_season=_selected_season(media, season_number),
                    score_snapshot=cached_scores,
                )

        queries = build_search_queries(media, season_number)
        query_results = await asyncio.gather(
            *(self._query_pansou(query) for query in queries),
            return_exceptions=True,
        )
        successful = [
            (query, result)
            for query, result in zip(queries, query_results, strict=True)
            if isinstance(result, dict)
        ]
        warnings = [
            f"pansou_query_failed:{index + 1}"
            for index, result in enumerate(query_results)
            if isinstance(result, Exception)
        ]
        if not successful:
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
                )
            raise SearchUnavailable("pansou_unavailable")

        complete = len(successful) == len(query_results)
        normalized = self._normalize_results(successful, now)
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
                    fallback_results = await asyncio.gather(
                        *(self._query_pansou(query) for query in fallback_queries),
                        return_exceptions=True,
                    )
                    fallback_successful = [
                        (query, result)
                        for query, result in zip(
                            fallback_queries, fallback_results, strict=True
                        )
                        if isinstance(result, dict)
                    ]
                    complete = len(fallback_successful) == len(fallback_results)
                    if fallback_successful:
                        normalized = self._merge_normalized_results(
                            normalized,
                            self._normalize_results(fallback_successful, now),
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

        candidates = _limit_magnet_resources(candidates)

        if not complete:
            warnings = _merge_warnings(warnings, ["partial_upstream"])
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
                )
            async with self._session_factory() as session:
                resources = await self._persist_resources(session, candidates, now)
                await session.commit()
            return self._response(
                media,
                resources,
                now,
                None,
                cached=False,
                warnings=warnings,
                selected_season=_selected_season(media, season_number),
                score_snapshot=_resource_score_snapshot(resources),
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
                session, cache_key, resources, score_snapshot, warnings, now
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
            )

    async def _query_pansou(self, query: str) -> dict:
        async with self._pansou_limit:
            return await self._pansou.search(query)

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
        expires_at = now + STALE_CACHE_AGE
        values = []
        for resource in resources:
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
                    else None,
                    "name": resource.name,
                    "size_bytes": resource.size_bytes,
                    "seeders": resource.seeders,
                    "source": resource.source,
                    "captured_at": resource.captured_at,
                    "expires_at": expires_at,
                    "metadata_json": json.dumps(resource.metadata, ensure_ascii=False),
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
            select(Resource).where(Resource.canonical_key.in_(canonical_keys))
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
            "fetched_at": now,
            "expires_at": now + STALE_CACHE_AGE,
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
    ) -> SearchResponse:
        return SearchResponse(
            movie=movie,
            results=[_resource_summary(item, score_snapshot) for item in resources],
            warnings=warnings or [],
            cached=cached,
            cache_age_seconds=(
                max(0, int(_age(cache.fetched_at, now).total_seconds()))
                if cache
                else None
            ),
            selected_season=selected_season,
        )


def _merge_normalized(
    existing: NormalizedResource,
    candidate: NormalizedResource,
) -> NormalizedResource:
    queries = list(existing.metadata.get("search_queries", []))
    for query in candidate.metadata.get("search_queries", []):
        if query not in queries:
            queries.append(query)
    sources = list(existing.metadata.get("sources", []))
    for source in candidate.metadata.get("sources", []):
        if source not in sources:
            sources.append(source)
    merged = merge_normalized_resources(existing, candidate)
    merged.metadata["search_queries"] = queries
    merged.metadata["sources"] = sources
    return merged


def _stored_warnings(cache: SearchCache) -> list[str]:
    try:
        warnings = json.loads(cache.warnings_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(warnings, list):
        return []
    return [item for item in warnings if isinstance(item, str)]


def _resource_summary(
    item: Resource,
    score_snapshot: Mapping[str, Mapping[str, int]] | None,
) -> ResourceSummary:
    metadata = _resource_metadata(item)
    size_source = metadata.get("size_source")
    seeders_source = metadata.get("seeders_source")
    observed_at = metadata.get("seeders_observed_at")
    return ResourceSummary(
        resource_id=item.id,
        kind=item.kind,
        name=item.name,
        size_bytes=item.size_bytes,
        seeders=item.seeders,
        source=item.source,
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
    return list({item.canonical_key: item for item in resources}.values())


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
    resources: list[NormalizedResource], limit: int = 30
) -> list[NormalizedResource]:
    magnets = [item for item in resources if item.kind == ResourceKind.MAGNET]
    allowed = {item.canonical_key for item in magnets[:limit]}
    return [
        item
        for item in resources
        if item.kind == ResourceKind.SHARE or item.canonical_key in allowed
    ]


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
