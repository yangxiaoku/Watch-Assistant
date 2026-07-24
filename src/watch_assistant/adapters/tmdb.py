"""TMDB movie and television metadata adapter."""

import re

import httpx

from watch_assistant.schemas import (
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    SeasonMetadata,
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

    async def get_movie(self, tmdb_id: int) -> MovieMetadata:
        return await self.get_media(tmdb_id, MediaType.MOVIE)

    async def get_media(
        self, tmdb_id: int, media_type: MediaType
    ) -> MovieMetadata:
        payload = await self._get(f"/{media_type.value}/{tmdb_id}")
        return _parse_media(payload, tmdb_id=tmdb_id, media_type=media_type)

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
        payload = await self._get(
            f"/{media_type.value}/{feed}", params={"page": page}
        )
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
        payload = await self._get(
            f"/discover/{media_type.value}", params=params
        )
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
        payload = await self._get(
            "/search/movie", params={"query": query, "page": 1}
        )
        return _parse_media_collection(
            payload, media_type=MediaType.MOVIE
        ).results

    async def search_media(
        self, query: str, *, page: int = 1
    ) -> MovieCollectionResponse:
        payload = await self._get(
            "/search/multi", params={"query": query, "page": page}
        )
        return _parse_multi_collection(payload)

    async def get_alternative_titles(
        self,
        tmdb_id: int,
        media_type: MediaType = MediaType.MOVIE,
    ) -> list[str]:
        payload = await self._get(
            f"/{media_type.value}/{tmdb_id}/alternative_titles"
        )
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
            by_region.setdefault(
                region if isinstance(region, str) else "", []
            ).append(title.strip())
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
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> dict:
        try:
            response = await self._client.get(
                path,
                params={
                    "api_key": self._api_key,
                    "language": "zh-CN",
                    **(params or {}),
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TmdbError("TMDB request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TmdbError("Unexpected TMDB response shape") from exc
        if not isinstance(payload, dict):
            raise TmdbError("Unexpected TMDB response shape")
        return payload

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
    date_field = (
        "release_date" if media_type == MediaType.MOVIE else "first_air_date"
    )
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
        seasons=_parse_seasons(payload, media_type) if include_seasons else [],
    )


def _parse_seasons(payload: dict, media_type: MediaType) -> list[SeasonMetadata]:
    if media_type != MediaType.TV:
        return []
    seasons = payload.get("seasons")
    if not isinstance(seasons, list):
        return []
    parsed: list[SeasonMetadata] = []
    for item in seasons:
        if not isinstance(item, dict) or not isinstance(item.get("season_number"), int):
            continue
        parsed.append(
            SeasonMetadata(
                season_number=item["season_number"],
                name=item.get("name")
                if isinstance(item.get("name"), str)
                else f"Season {item['season_number']}",
                episode_count=(
                    item["episode_count"]
                    if isinstance(item.get("episode_count"), int)
                    and item["episode_count"] >= 0
                    else 0
                ),
                air_date=item.get("air_date")
                if isinstance(item.get("air_date"), str)
                else None,
                poster_path=item.get("poster_path")
                if isinstance(item.get("poster_path"), str)
                else None,
            )
        )
    return parsed


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
            (media.title, (f"{media.title} 第{season_number}季", f"{media.title} S{season}")),
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
