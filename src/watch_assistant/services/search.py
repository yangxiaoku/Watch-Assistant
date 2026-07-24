"""Search orchestration, resource persistence, and cache handling."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient, build_search_queries
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import Resource, SearchCache
from watch_assistant.schemas import (
    HomeCatalogResponse,
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    ResourceSummary,
    SearchResponse,
)
from watch_assistant.services.normalize import (
    merge_normalized_resources,
    normalize_pansou,
)

FRESH_CACHE_AGE = timedelta(minutes=30)
STALE_CACHE_AGE = timedelta(hours=24)


class SearchUnavailable(RuntimeError):
    pass


def make_cache_key(
    tmdb_id: int, media_type: MediaType = MediaType.MOVIE
) -> str:
    return f"tmdb:{media_type.value}:{tmdb_id}:queries:v3"


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tmdb_client: TmdbClient,
        pansou_client: PanSouClient,
        crypto: SecretCrypto,
        share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    ) -> None:
        self._session_factory = session_factory
        self._tmdb = tmdb_client
        self._pansou = pansou_client
        self._crypto = crypto
        self._share_domains = share_domains

    async def get_movie(self, tmdb_id: int) -> MovieMetadata:
        return await self._tmdb.get_movie(tmdb_id)

    async def get_media(
        self, tmdb_id: int, media_type: MediaType
    ) -> MovieMetadata:
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

    async def get_popular_page(
        self, page: int
    ) -> MovieCollectionResponse:
        return await self._tmdb.get_feed_page("popular", page=page)

    async def search_movies(self, query: str) -> list[MovieMetadata]:
        return await self._tmdb.search_movies(query)

    async def search_media(
        self, query: str, page: int
    ) -> MovieCollectionResponse:
        return await self._tmdb.search_media(query, page=page)

    async def search(
        self,
        tmdb_id: int,
        *,
        media_type: MediaType = MediaType.MOVIE,
        refresh: bool = False,
    ) -> SearchResponse:
        movie = await self.get_media(tmdb_id, media_type)
        cache_key = make_cache_key(tmdb_id, media_type)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            cache = await session.get(SearchCache, cache_key)
            if cache and not refresh and _age(cache.fetched_at, now) <= FRESH_CACHE_AGE:
                resources = await self._load_cached_resources(session, cache)
                return self._response(movie, resources, now, cache, cached=True)

            queries = build_search_queries(movie)
            query_results = await asyncio.gather(
                *(self._pansou.search(query) for query in queries),
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
                if cache and _age(cache.fetched_at, now) <= STALE_CACHE_AGE:
                    resources = await self._load_cached_resources(session, cache)
                    warnings.append("stale_cache")
                    response = self._response(
                        movie, resources, now, cache, cached=True, warnings=warnings
                    )
                    return response
                raise SearchUnavailable("pansou_unavailable")

            normalized_by_key = {}
            for query, result in successful:
                for resource in normalize_pansou(
                    result,
                    share_domains=self._share_domains,
                    captured_at=now,
                ):
                    resource.metadata["search_queries"] = [query]
                    resource.metadata["sources"] = [resource.source]
                    existing = normalized_by_key.get(resource.canonical_key)
                    if existing is None:
                        normalized_by_key[resource.canonical_key] = resource
                        continue

                    matched_queries = list(existing.metadata["search_queries"])
                    if query not in matched_queries:
                        matched_queries.append(query)
                    sources = list(existing.metadata["sources"])
                    if resource.source not in sources:
                        sources.append(resource.source)
                    merged = merge_normalized_resources(existing, resource)
                    merged.metadata["search_queries"] = matched_queries
                    merged.metadata["sources"] = sources
                    normalized_by_key[resource.canonical_key] = merged
            normalized = list(normalized_by_key.values())
            resources = await self._persist_resources(session, normalized, now)
            await self._persist_cache(session, cache_key, resources, warnings, now)
            await session.commit()
            cache = await session.get(SearchCache, cache_key)
            return self._response(
                movie, resources, now, cache, cached=False, warnings=warnings
            )

    async def _load_cached_resources(
        self, session: AsyncSession, cache: SearchCache
    ) -> list[Resource]:
        try:
            resource_ids = json.loads(cache.resource_ids_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(resource_ids, list) or not all(
            isinstance(resource_id, str) for resource_id in resource_ids
        ):
            return []
        rows = await session.scalars(
            select(Resource).where(Resource.id.in_(resource_ids))
        )
        by_id = {resource.id: resource for resource in rows}
        return [by_id[resource_id] for resource_id in resource_ids if resource_id in by_id]

    async def _persist_resources(self, session, resources, now):
        if not resources:
            return []
        expires_at = now + STALE_CACHE_AGE
        values = []
        for resource in resources:
            resource_id = "res_" + sha256(resource.canonical_key.encode()).hexdigest()[:24]
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

    async def _persist_cache(self, session, cache_key, resources, warnings, now):
        values = {
            "cache_key": cache_key,
            "resource_ids_json": json.dumps([item.id for item in resources]),
            "warnings_json": json.dumps(warnings),
            "fetched_at": now,
            "expires_at": now + STALE_CACHE_AGE,
        }
        stmt = insert(SearchCache).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[SearchCache.cache_key],
                set_={key: getattr(stmt.excluded, key) for key in values if key != "cache_key"},
            )
        )

    @staticmethod
    def _response(
        movie,
        resources,
        now,
        cache,
        *,
        cached,
        warnings=None,
    ) -> SearchResponse:
        return SearchResponse(
            movie=movie,
            results=[
                ResourceSummary(
                    resource_id=item.id,
                    kind=item.kind,
                    name=item.name,
                    size_bytes=item.size_bytes,
                    seeders=item.seeders,
                    source=item.source,
                    captured_at=_as_utc(item.captured_at),
                )
                for item in resources
            ],
            warnings=warnings or [],
            cached=cached,
            cache_age_seconds=(
                max(0, int(_age(cache.fetched_at, now).total_seconds()))
                if cache
                else None
            ),
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age(value: datetime, now: datetime) -> timedelta:
    return now - _as_utc(value)
