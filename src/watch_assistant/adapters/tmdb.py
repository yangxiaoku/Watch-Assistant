"""TMDB movie metadata adapter."""

import re

import httpx

from watch_assistant.schemas import MovieMetadata

TMDB_BASE_URL = "https://api.themoviedb.org/3"
MOVIE_FEEDS = {"popular", "now_playing", "upcoming", "top_rated"}
DISCOVER_SORTS = {
    "popular": "popularity.desc",
    "rating": "vote_average.desc",
    "release": "primary_release_date.desc",
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
        payload = await self._get(f"/movie/{tmdb_id}")
        return _parse_movie(payload, tmdb_id=tmdb_id)

    async def get_popular(self) -> list[MovieMetadata]:
        return await self.get_feed("popular")

    async def get_feed(self, feed: str) -> list[MovieMetadata]:
        if feed not in MOVIE_FEEDS:
            raise ValueError("Unsupported movie feed")
        payload = await self._get(f"/movie/{feed}", params={"page": 1})
        return _parse_movie_collection(payload)

    async def discover_movies(
        self,
        *,
        genre_id: int | None = None,
        year: int | None = None,
        sort: str = "popular",
    ) -> list[MovieMetadata]:
        if sort not in DISCOVER_SORTS:
            raise ValueError("Unsupported movie sort")
        params: dict[str, str | int] = {
            "page": 1,
            "sort_by": DISCOVER_SORTS[sort],
            "include_adult": "false",
            "include_video": "false",
        }
        if genre_id is not None:
            params["with_genres"] = genre_id
        if year is not None:
            params["primary_release_year"] = year
        if sort == "rating":
            params["vote_count.gte"] = 200
        payload = await self._get("/discover/movie", params=params)
        return _parse_movie_collection(payload)

    async def search_movies(self, query: str) -> list[MovieMetadata]:
        payload = await self._get(
            "/search/movie", params={"query": query, "page": 1}
        )
        return _parse_movie_collection(payload)

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


def _parse_movie_collection(payload: dict) -> list[MovieMetadata]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise TmdbError("Unexpected TMDB response shape")
    movies: list[MovieMetadata] = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        try:
            movies.append(_parse_movie(item, tmdb_id=item["id"]))
        except TmdbError:
            continue
    return movies


def _parse_movie(payload: dict, *, tmdb_id: int) -> MovieMetadata:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TmdbError("Unexpected TMDB response shape")
    original_title = payload.get("original_title")
    if not isinstance(original_title, str):
        original_title = None
    release_date = payload.get("release_date")
    release_year = None
    if isinstance(release_date, str) and re.match(r"^\d{4}-", release_date):
        release_year = int(release_date[:4])
    vote_average = payload.get("vote_average")
    if not isinstance(vote_average, (int, float)):
        vote_average = None

    return MovieMetadata(
        tmdb_id=tmdb_id,
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
    )


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


def build_search_queries(movie: MovieMetadata) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for candidate in (movie.title, movie.original_title):
        if not candidate:
            continue
        normalized_title = " ".join(candidate.split())
        query = (
            f"{normalized_title} {movie.release_year}"
            if movie.release_year is not None
            else normalized_title
        )
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            queries.append(query)
        if len(queries) == 2:
            break
    return queries
