"""TMDB movie metadata adapter."""

import re

import httpx

from watch_assistant.schemas import MovieMetadata

TMDB_BASE_URL = "https://api.themoviedb.org/3"


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
        try:
            response = await self._client.get(
                f"/movie/{tmdb_id}",
                params={"api_key": self._api_key, "language": "zh-CN"},
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
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
