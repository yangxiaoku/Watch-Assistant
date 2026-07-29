"""Cached, read-only TMDB season metadata service."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.tmdb import TmdbClient, TmdbError, TmdbNotFoundError
from watch_assistant.models import SeasonMetadataCache
from watch_assistant.schemas import SeasonDetailResponse

SEASON_CACHE_TTL = timedelta(hours=12)


class SeasonMetadataError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SeasonMetadataService:
    """Keep season metadata independent from resource search snapshots."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmdb_client: TmdbClient,
        *,
        cache_ttl: timedelta = SEASON_CACHE_TTL,
    ) -> None:
        if cache_ttl <= timedelta(0):
            raise ValueError("cache_ttl must be positive")
        self._session_factory = session_factory
        self._tmdb = tmdb_client
        self._cache_ttl = cache_ttl
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(
        self,
        series_tmdb_id: int,
        season_number: int,
        *,
        language: str = "zh-CN",
        fallback_language: str = "en-US",
        refresh: bool = False,
    ) -> SeasonDetailResponse:
        _validate_identity(series_tmdb_id, season_number, language, fallback_language)
        cache_key = _cache_key(series_tmdb_id, season_number, language)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = await self._load(cache_key)
            now = datetime.now(UTC)
            if (
                cached is not None
                and not refresh
                and _as_utc(cached.expires_at) > now
            ):
                return _response_from_payload(cached.payload_json, cached=True, stale=False)
            try:
                detail = await self._fetch(
                    series_tmdb_id,
                    season_number,
                    language=language,
                    fallback_language=fallback_language,
                )
            except TmdbNotFoundError:
                raise SeasonMetadataError("season_not_found") from None
            except TmdbError:
                if cached is not None:
                    return _response_from_payload(
                        cached.payload_json,
                        cached=True,
                        stale=True,
                        extra_warning="season_metadata_stale",
                    )
                raise SeasonMetadataError("season_metadata_unavailable") from None
            await self._store(cache_key, series_tmdb_id, season_number, language, detail)
            return detail.model_copy(update={"cached": False, "stale": False})

    async def _fetch(
        self,
        series_tmdb_id: int,
        season_number: int,
        *,
        language: str,
        fallback_language: str,
    ) -> SeasonDetailResponse:
        detail = await self._tmdb.get_season(
            series_tmdb_id, season_number, language=language
        )
        if (
            not detail.overview
            and fallback_language != language
            and fallback_language
        ):
            try:
                fallback = await self._tmdb.get_season(
                    series_tmdb_id, season_number, language=fallback_language
                )
            except TmdbError:
                fallback = None
            if fallback is not None and fallback.overview:
                detail = detail.model_copy(
                    update={
                        "overview": fallback.overview,
                        "overview_language": fallback.overview_language
                        or fallback_language,
                        "warnings": ["season_overview_language_fallback"],
                    }
                )
        return detail

    async def _load(self, cache_key: str) -> SeasonMetadataCache | None:
        async with self._session_factory() as session:
            return await session.get(SeasonMetadataCache, cache_key)

    async def _store(
        self,
        cache_key: str,
        series_tmdb_id: int,
        season_number: int,
        language: str,
        detail: SeasonDetailResponse,
    ) -> None:
        now = datetime.now(UTC)
        stored = detail.model_copy(update={"cached": False, "stale": False, "warnings": []})
        payload = json.dumps(stored.model_dump(mode="json"), ensure_ascii=False)
        async with self._session_factory() as session:
            cache = await session.get(SeasonMetadataCache, cache_key)
            if cache is None:
                cache = SeasonMetadataCache(
                    cache_key=cache_key,
                    series_tmdb_id=series_tmdb_id,
                    season_number=season_number,
                    language=language,
                    payload_json=payload,
                    fetched_at=now,
                    expires_at=now + self._cache_ttl,
                )
                session.add(cache)
            else:
                cache.payload_json = payload
                cache.fetched_at = now
                cache.expires_at = now + self._cache_ttl
            await session.commit()


def _cache_key(series_tmdb_id: int, season_number: int, language: str) -> str:
    return f"tmdb:tv:{series_tmdb_id}:season:{season_number}:language:{language}:v1"


def _response_from_payload(
    payload: str,
    *,
    cached: bool,
    stale: bool,
    extra_warning: str | None = None,
) -> SeasonDetailResponse:
    try:
        parsed = SeasonDetailResponse.model_validate(json.loads(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SeasonMetadataError("season_metadata_cache_invalid") from None
    warnings = list(parsed.warnings)
    if extra_warning and extra_warning not in warnings:
        warnings.append(extra_warning)
    return parsed.model_copy(update={"cached": cached, "stale": stale, "warnings": warnings})


def _validate_identity(
    series_tmdb_id: int,
    season_number: int,
    language: str,
    fallback_language: str,
) -> None:
    if not isinstance(series_tmdb_id, int) or isinstance(series_tmdb_id, bool) or series_tmdb_id < 1:
        raise SeasonMetadataError("invalid_series_id")
    if not isinstance(season_number, int) or isinstance(season_number, bool) or season_number < 0:
        raise SeasonMetadataError("invalid_season_number")
    for value in (language, fallback_language):
        if not isinstance(value, str) or not value or len(value) > 32 or any(
            char in value for char in ("/", "\\", "\x00")
        ):
            raise SeasonMetadataError("invalid_language")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
