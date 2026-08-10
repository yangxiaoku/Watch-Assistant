"""TMDB movie and television metadata adapter."""

import asyncio
import re
from datetime import UTC, datetime

import httpx

from watch_assistant.schemas import (
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    SeasonDetailResponse,
    SeasonEpisodeMetadata,
    SeasonMetadata,
)
from watch_assistant.services.media_matcher import (
    MediaKind,
    MediaMatchInput,
    TmdbCandidate,
)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
ALTERNATIVE_TITLE_REGIONS = ("CN", "HK", "TW")
MEDIA_FEEDS = {
    MediaType.MOVIE: {"popular", "now_playing", "upcoming", "top_rated"},
    MediaType.TV: {"popular", "on_the_air", "airing_today", "top_rated"},
}
DISCOVER_SORTS = {
    MediaType.MOVIE: {
        "popular": "popularity.desc",
        "rating": "vote_average.desc",
        "release": "primary_release_date.desc",
    },
    MediaType.TV: {
        "popular": "popularity.desc",
        "rating": "vote_average.desc",
        "release": "first_air_date.desc",
    },
}


class TmdbError(RuntimeError):
    pass


class TmdbAuthError(TmdbError):
    pass


class TmdbNotFoundError(TmdbError):
    pass


class TmdbRateLimitedError(TmdbError):
    """429 限流:与普通失败区分,携带服务端建议的等待秒数。"""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("TMDB rate limited")
        self.retry_after_seconds = retry_after_seconds


# 429/5xx 的有界重试次数与指数退避基数(1s → 2s)。
_TMDB_RETRY_ATTEMPTS = 3
_TMDB_BACKOFF_BASE = 1.0
# 候选详情富化的并发上限:每个候选 2 个详情请求(zh + en),
# 并发 4 个候选 ≈ 8 个在途请求,配合 _get 内的 429 退避重试
# 既能显著缩短串行富化的耗时,又不至于打满免费配额(约 40 req/10s)。
_TMDB_DETAIL_CONCURRENCY = 4


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TMDB_BASE_URL,
        timeout: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))

    def set_api_key(self, api_key: str) -> None:
        self._api_key = api_key

    update_api_key = set_api_key

    @property
    def api_key_configured(self) -> bool:
        return bool(self._api_key)

    async def validate_api_key(self, api_key: str) -> None:
        """Perform exactly one read-only request for a candidate key."""
        await self._get("/configuration", api_key=api_key)

    async def get_movie(self, tmdb_id: int) -> MovieMetadata:
        return await self.get_media(tmdb_id, MediaType.MOVIE)

    async def get_media(self, tmdb_id: int, media_type: MediaType) -> MovieMetadata:
        payload = await self._get(f"/{media_type.value}/{tmdb_id}")
        return _parse_media(payload, tmdb_id=tmdb_id, media_type=media_type)

    async def get_season(
        self,
        tmdb_id: int,
        season_number: int,
        *,
        language: str = "zh-CN",
    ) -> SeasonDetailResponse:
        if tmdb_id < 1 or season_number < 0:
            raise ValueError("Invalid TMDB season identity")
        payload = await self._get(
            f"/tv/{tmdb_id}/season/{season_number}", language=language
        )
        return _parse_season(
            payload,
            series_tmdb_id=tmdb_id,
            season_number=season_number,
            language=language,
        )

    async def get_popular(self) -> list[MovieMetadata]:
        return await self.get_feed("popular")

    async def get_feed(
        self, feed: str, media_type: MediaType = MediaType.MOVIE
    ) -> list[MovieMetadata]:
        return (await self.get_feed_page(feed, media_type=media_type)).results

    async def get_feed_page(
        self,
        feed: str,
        *,
        media_type: MediaType = MediaType.MOVIE,
        page: int = 1,
    ) -> MovieCollectionResponse:
        if feed not in MEDIA_FEEDS[media_type]:
            raise ValueError("Unsupported media feed")
        payload = await self._get(f"/{media_type.value}/{feed}", params={"page": page})
        return _parse_media_collection(payload, media_type=media_type)

    async def discover_media(
        self,
        *,
        media_type: MediaType,
        genre_id: int | None = None,
        year: int | None = None,
        sort: str = "popular",
        page: int = 1,
    ) -> MovieCollectionResponse:
        if sort not in DISCOVER_SORTS[media_type]:
            raise ValueError("Unsupported media sort")
        params: dict[str, str | int] = {
            "page": page,
            "sort_by": DISCOVER_SORTS[media_type][sort],
            "include_adult": "false",
            "include_video": "false",
        }
        if genre_id is not None:
            params["with_genres"] = genre_id
        if year is not None:
            year_field = (
                "primary_release_year"
                if media_type == MediaType.MOVIE
                else "first_air_date_year"
            )
            params[year_field] = year
        if sort == "rating":
            params["vote_count.gte"] = 200
        payload = await self._get(f"/discover/{media_type.value}", params=params)
        return _parse_media_collection(payload, media_type=media_type)

    async def discover_movies(
        self,
        *,
        genre_id: int | None = None,
        year: int | None = None,
        sort: str = "popular",
        page: int = 1,
    ) -> list[MovieMetadata]:
        return (
            await self.discover_media(
                media_type=MediaType.MOVIE,
                genre_id=genre_id,
                year=year,
                sort=sort,
                page=page,
            )
        ).results

    async def search_movies(self, query: str) -> list[MovieMetadata]:
        payload = await self._get("/search/movie", params={"query": query, "page": 1})
        return _parse_media_collection(payload, media_type=MediaType.MOVIE).results

    async def search_media(
        self, query: str, *, page: int = 1
    ) -> MovieCollectionResponse:
        payload = await self._get(
            "/search/multi", params={"query": query, "page": page}
        )
        return _parse_multi_collection(payload)

    async def search_candidates(
        self, query: MediaMatchInput, *, limit: int = 8
    ) -> list[TmdbCandidate]:
        """Return bounded, redacted candidates for the organization matcher.

        Search results do not consistently include country or season metadata,
        so each bounded candidate is enriched with its read-only detail payload.
        Missing enrichment is retained as incomplete evidence and is therefore
        handled conservatively by ``TmdbMatcher``.
        """

        if not isinstance(query, MediaMatchInput) or not query.title:
            return []
        if limit < 1 or limit > 20:
            raise ValueError("candidate limit out of range")
        payload = await self._get(
            "/search/multi", params={"query": query.title, "page": 1}
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise TmdbError("Unexpected TMDB response shape")
        # 先过滤媒体类型命中项:与旧串行实现一致,非命中项不发起任何请求。
        matching: list[dict] = []
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            media_type = item.get("media_type")
            if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
                continue
            if (
                query.media_type_hint in {MediaType.MOVIE.value, MediaType.TV.value}
                and media_type != query.media_type_hint
            ):
                continue
            matching.append(item)

        # 详情富化原先逐个串行(最多 2*limit+1 个请求),改为按 limit
        # 分片并发:每片内部用信号量限制并发候选数,保持"按结果顺序取
        # 前 limit 个有效候选"的既有语义;429 限流仍由 _get 的退避重试兜底。
        semaphore = asyncio.Semaphore(_TMDB_DETAIL_CONCURRENCY)
        candidates: list[TmdbCandidate] = []
        offset = 0
        while len(candidates) < limit and offset < len(matching):
            batch = matching[offset : offset + limit]
            offset += limit
            enriched = await asyncio.gather(
                *(self._enrich_candidate(item, semaphore) for item in batch)
            )
            for candidate_payload in enriched:
                if candidate_payload is None:
                    continue
                try:
                    candidates.append(TmdbCandidate.from_payload(candidate_payload))
                except (TypeError, ValueError):
                    continue
                if len(candidates) >= limit:
                    break
        return candidates

    async def _enrich_candidate(
        self, item: dict, semaphore: asyncio.Semaphore
    ) -> dict | None:
        """抓取一个候选的详情并按匹配器契约组装负载。

        本地化详情失败时降级为搜索摘要(缺失详情的证据由 TmdbMatcher
        按保守策略处理);本地化成功后才请求英文详情(种子文件名常为
        英文,无法匹配本地化标题)。返回 None 表示负载无效,由调用方跳过。
        """
        async with semaphore:
            media_type = item.get("media_type")
            try:
                detail = await self._get(f"/{media_type}/{item['id']}")
            except TmdbError:
                # 搜索摘要同样可用,但缺失详情必须让候选证据不完整。
                detail = item
                english_detail: dict | None = None
            else:
                try:
                    english_detail = await self._get(
                        f"/{media_type}/{item['id']}", params={"language": "en-US"}
                    )
                except TmdbError:
                    english_detail = None
        candidate_payload = dict(item)
        candidate_payload.update(detail)
        # 保留本地化搜索结果的标题用于展示;英文详情标题只用于匹配。
        # TV 响应用 "name" 字段。
        localized_title = item.get("title") or item.get("name")
        if localized_title:
            candidate_payload["title"] = localized_title
        if isinstance(english_detail, dict):
            english_title = english_detail.get("title") or english_detail.get("name")
            if isinstance(english_title, str):
                candidate_payload["english_title"] = english_title
        candidate_payload["id"] = item["id"]
        candidate_payload["media_type"] = media_type
        candidate_payload["kind"] = (
            MediaKind.MOVIE.value
            if media_type == MediaType.MOVIE.value
            else MediaKind.TV.value
        )
        countries = _candidate_countries(detail)
        if countries:
            candidate_payload["origin_country"] = countries
        if media_type == MediaType.TV.value:
            seasons = detail.get("seasons")
            if isinstance(seasons, list):
                candidate_payload["seasons"] = seasons
        return candidate_payload

    async def get_alternative_titles(
        self,
        tmdb_id: int,
        media_type: MediaType = MediaType.MOVIE,
    ) -> list[str]:
        payload = await self._get(f"/{media_type.value}/{tmdb_id}/alternative_titles")
        key = "titles" if media_type == MediaType.MOVIE else "results"
        titles = payload.get(key)
        if not isinstance(titles, list):
            raise TmdbError("Unexpected TMDB response shape")
        by_region: dict[str, list[str]] = {}
        for item in titles:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            region = item.get("iso_3166_1")
            if not isinstance(title, str) or not title.strip():
                continue
            by_region.setdefault(region if isinstance(region, str) else "", []).append(
                title.strip()
            )
        ordered: list[str] = []
        seen: set[str] = set()
        for region in (*ALTERNATIVE_TITLE_REGIONS, *by_region):
            for title in by_region.get(region, []):
                key = title.casefold()
                if key not in seen:
                    seen.add(key)
                    ordered.append(title)
        return ordered

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        api_key: str | None = None,
        language: str = "zh-CN",
    ) -> dict:
        # 429/5xx 有界退避重试:免费配额约 40 req/10s,富化批量请求极易触顶。
        # 尊重 Retry-After,超限后按 TmdbRateLimitedError 交给调用方降级。
        last_error: Exception | None = None
        for attempt in range(_TMDB_RETRY_ATTEMPTS):
            try:
                response = await self._client.get(
                    path,
                    params={
                        "api_key": self._api_key if api_key is None else api_key,
                        "language": language,
                        **(params or {}),
                    },
                    timeout=self._timeout,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {401, 403}:
                    raise TmdbAuthError("TMDB credential rejected") from None
                if status == 404:
                    raise TmdbNotFoundError("TMDB resource not found") from None
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After")
                    seconds: float | None = None
                    if retry_after is not None:
                        try:
                            seconds = max(0.0, float(retry_after))
                        except ValueError:
                            seconds = None
                    if attempt + 1 < _TMDB_RETRY_ATTEMPTS:
                        last_error = TmdbRateLimitedError(seconds)
                        await asyncio.sleep(seconds if seconds is not None else _TMDB_BACKOFF_BASE * 2**attempt)
                        continue
                    raise TmdbRateLimitedError(seconds) from None
                if attempt + 1 < _TMDB_RETRY_ATTEMPTS:
                    last_error = exc
                    await asyncio.sleep(_TMDB_BACKOFF_BASE * 2**attempt)
                    continue
                raise TmdbError("TMDB request failed") from None
            except httpx.HTTPError as exc:
                if attempt + 1 < _TMDB_RETRY_ATTEMPTS:
                    last_error = exc
                    await asyncio.sleep(_TMDB_BACKOFF_BASE * 2**attempt)
                    continue
                raise TmdbError("TMDB request failed") from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise TmdbError("Unexpected TMDB response shape") from exc
            if not isinstance(payload, dict):
                raise TmdbError("Unexpected TMDB response shape")
            return payload
        raise TmdbError("TMDB request failed") from last_error

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_media_collection(
    payload: dict, *, media_type: MediaType
) -> MovieCollectionResponse:
    results = payload.get("results")
    if not isinstance(results, list):
        raise TmdbError("Unexpected TMDB response shape")
    items: list[MovieMetadata] = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        try:
            items.append(
                _parse_media(
                    item,
                    tmdb_id=item["id"],
                    media_type=media_type,
                    include_seasons=False,
                )
            )
        except TmdbError:
            continue
    return MovieCollectionResponse(
        results=items,
        page=_positive_int(payload.get("page"), 1),
        total_pages=min(_positive_int(payload.get("total_pages"), 1), 500),
        total_results=_nonnegative_int(payload.get("total_results"), len(items)),
    )


def _parse_multi_collection(payload: dict) -> MovieCollectionResponse:
    results = payload.get("results")
    if not isinstance(results, list):
        raise TmdbError("Unexpected TMDB response shape")
    items: list[MovieMetadata] = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        try:
            media_type = MediaType(item.get("media_type"))
        except ValueError:
            continue
        try:
            items.append(
                _parse_media(
                    item,
                    tmdb_id=item["id"],
                    media_type=media_type,
                    include_seasons=False,
                )
            )
        except TmdbError:
            continue
    return MovieCollectionResponse(
        results=items,
        page=_positive_int(payload.get("page"), 1),
        total_pages=min(_positive_int(payload.get("total_pages"), 1), 500),
        total_results=_nonnegative_int(payload.get("total_results"), len(items)),
    )


def _parse_media(
    payload: dict,
    *,
    tmdb_id: int,
    media_type: MediaType,
    include_seasons: bool = True,
) -> MovieMetadata:
    title_field = "title" if media_type == MediaType.MOVIE else "name"
    original_field = (
        "original_title" if media_type == MediaType.MOVIE else "original_name"
    )
    date_field = "release_date" if media_type == MediaType.MOVIE else "first_air_date"
    title = payload.get(title_field)
    if not isinstance(title, str) or not title.strip():
        raise TmdbError("Unexpected TMDB response shape")
    original_title = payload.get(original_field)
    if not isinstance(original_title, str):
        original_title = None
    release_date = payload.get(date_field)
    release_year = None
    if isinstance(release_date, str) and re.match(r"^\d{4}-", release_date):
        release_year = int(release_date[:4])
    vote_average = payload.get("vote_average")
    if not isinstance(vote_average, (int, float)):
        vote_average = None

    return MovieMetadata(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title.strip(),
        original_title=original_title.strip() if original_title else None,
        release_year=release_year,
        overview=payload.get("overview")
        if isinstance(payload.get("overview"), str)
        else None,
        poster_path=payload.get("poster_path")
        if isinstance(payload.get("poster_path"), str)
        else None,
        backdrop_path=payload.get("backdrop_path")
        if isinstance(payload.get("backdrop_path"), str)
        else None,
        genre_ids=_parse_genre_ids(payload),
        vote_average=vote_average,
        adult=payload.get("adult") is True,
        seasons=_parse_seasons(payload, media_type) if include_seasons else [],
    )


def _parse_seasons(payload: dict, media_type: MediaType) -> list[SeasonMetadata]:
    if media_type != MediaType.TV:
        return []
    seasons = payload.get("seasons")
    if not isinstance(seasons, list):
        return []
    by_number: dict[int, SeasonMetadata] = {}
    for item in seasons:
        season_number = item.get("season_number") if isinstance(item, dict) else None
        if (
            not isinstance(season_number, int)
            or isinstance(season_number, bool)
            or season_number < 0
        ):
            continue
        raw_name = item.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name:
            name = f"Season {season_number}"
        episode_count = item.get("episode_count")
        if (
            not isinstance(episode_count, int)
            or isinstance(episode_count, bool)
            or episode_count < 0
        ):
            episode_count = 0
        by_number.setdefault(
            season_number,
            SeasonMetadata(
                season_number=season_number,
                name=name,
                episode_count=episode_count,
                air_date=item.get("air_date")
                if isinstance(item.get("air_date"), str)
                else None,
                poster_path=item.get("poster_path")
                if isinstance(item.get("poster_path"), str)
                else None,
                tmdb_season_id=(
                    item.get("id")
                    if isinstance(item.get("id"), int) and item["id"] > 0
                    else None
                ),
                overview_available=bool(
                    isinstance(item.get("overview"), str)
                    and item["overview"].strip()
                ),
            ),
        )
    return [by_number[number] for number in sorted(by_number)]


def _parse_season(
    payload: dict,
    *,
    series_tmdb_id: int,
    season_number: int,
    language: str,
) -> SeasonDetailResponse:
    raw_name = payload.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        name = "特别篇" if season_number == 0 else f"第 {season_number} 季"
    raw_overview = payload.get("overview")
    overview = raw_overview.strip() if isinstance(raw_overview, str) else None
    overview = overview or None
    episodes: list[SeasonEpisodeMetadata] = []
    raw_episodes = payload.get("episodes")
    if isinstance(raw_episodes, list):
        for item in raw_episodes:
            if not isinstance(item, dict):
                continue
            episode_number = item.get("episode_number")
            if (
                not isinstance(episode_number, int)
                or isinstance(episode_number, bool)
                or episode_number < 0
            ):
                continue
            raw_episode_name = item.get("name")
            episode_name = (
                raw_episode_name.strip()
                if isinstance(raw_episode_name, str)
                else f"第 {episode_number} 集"
            )
            episodes.append(
                SeasonEpisodeMetadata(
                    episode_number=episode_number,
                    name=episode_name or f"第 {episode_number} 集",
                    overview=_optional_text(item.get("overview")),
                    air_date=_optional_text(item.get("air_date")),
                    still_path=_optional_text(item.get("still_path")),
                    runtime=(
                        item["runtime"]
                        if isinstance(item.get("runtime"), int)
                        and not isinstance(item["runtime"], bool)
                        and item["runtime"] >= 0
                        else None
                    ),
                    vote_average=(
                        float(item["vote_average"])
                        if isinstance(item.get("vote_average"), (int, float))
                        and not isinstance(item.get("vote_average"), bool)
                        else None
                    ),
                )
            )
    episodes.sort(key=lambda item: item.episode_number)
    episode_count = payload.get("episode_count")
    if not isinstance(episode_count, int) or isinstance(episode_count, bool) or episode_count < 0:
        episode_count = len(episodes)
    vote_average = payload.get("vote_average")
    return SeasonDetailResponse(
        series_tmdb_id=series_tmdb_id,
        tmdb_season_id=(
            payload["id"]
            if isinstance(payload.get("id"), int) and payload["id"] > 0
            else None
        ),
        season_number=season_number,
        name=name,
        overview=overview,
        overview_language=language if overview else None,
        poster_path=_optional_text(payload.get("poster_path")),
        air_date=_optional_text(payload.get("air_date")),
        episode_count=episode_count,
        vote_average=(
            float(vote_average)
            if isinstance(vote_average, (int, float)) and not isinstance(vote_average, bool)
            else None
        ),
        fetched_at=datetime.now(UTC),
        episodes=episodes,
    )


def _candidate_countries(payload: dict) -> list[str]:
    values = payload.get("origin_country")
    if isinstance(values, list):
        countries = [item for item in values if isinstance(item, str)]
        if countries:
            return countries
    values = payload.get("production_countries")
    if not isinstance(values, list):
        return []
    return [
        item["iso_3166_1"]
        for item in values
        if isinstance(item, dict) and isinstance(item.get("iso_3166_1"), str)
    ]


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_genre_ids(payload: dict) -> list[int]:
    genre_ids = payload.get("genre_ids")
    if isinstance(genre_ids, list):
        return [item for item in genre_ids if isinstance(item, int)]
    genres = payload.get("genres")
    if isinstance(genres, list):
        return [
            item["id"]
            for item in genres
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]
    return []


def _positive_int(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and value > 0 else fallback


def _nonnegative_int(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and value >= 0 else fallback


def build_search_queries(
    media: MovieMetadata, season_number: int | None = None
) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for candidate in (media.title, media.original_title):
        if not candidate:
            continue
        normalized_title = " ".join(candidate.split())
        variants = [normalized_title]
        if media.release_year is not None:
            variants.append(f"{normalized_title} {media.release_year}")
        for query in variants:
            key = query.casefold()
            if key not in seen:
                seen.add(key)
                queries.append(query)
    queries = queries[:4]
    if media.media_type == MediaType.TV and season_number is not None:
        season = f"{season_number:02d}"
        for title, variants in (
            (
                media.title,
                (f"{media.title} 第{season_number}季", f"{media.title} S{season}"),
            ),
            (
                media.original_title,
                (
                    f"{media.original_title} Season {season_number}",
                    f"{media.original_title} S{season}",
                ),
            ),
        ):
            if not title:
                continue
            for query in variants:
                key = query.casefold()
                if key not in seen:
                    seen.add(key)
                    queries.append(query)
    return queries
