"""Movie and PanSou search routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.adapters.tmdb import TmdbError
from watch_assistant.schemas import (
    HomeCatalogResponse,
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    SearchRequest,
    SearchResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.search import (
    InvalidSeasonRequest,
    SearchService,
    SearchUnavailable,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


SearchServiceDependency = Annotated[SearchService, Depends(get_search_service)]


@router.get("/movies/home", response_model=HomeCatalogResponse)
async def get_home_catalog(
    service: SearchServiceDependency,
) -> HomeCatalogResponse:
    try:
        return await service.get_home_catalog()
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/movies/discover", response_model=MovieCollectionResponse)
async def discover_movies(
    service: SearchServiceDependency,
    genre_id: int | None = Query(default=None, ge=1),
    year: int | None = Query(default=None, ge=1900, le=2100),
    sort: Literal["popular", "rating", "release"] = "popular",
    page: int = Query(default=1, ge=1, le=500),
) -> MovieCollectionResponse:
    try:
        return await service.discover_media(
            media_type=MediaType.MOVIE,
            genre_id=genre_id,
            year=year,
            sort=sort,
            page=page,
        )
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/movies/popular", response_model=MovieCollectionResponse)
async def get_popular_movies(
    service: SearchServiceDependency,
    page: int = Query(default=1, ge=1, le=500),
) -> MovieCollectionResponse:
    try:
        return await service.get_popular_page(page)
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/movies/search", response_model=MovieCollectionResponse)
async def search_movies(
    service: SearchServiceDependency,
    query: str = Query(min_length=1, max_length=100),
) -> MovieCollectionResponse:
    try:
        return MovieCollectionResponse(results=await service.search_movies(query))
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/media/search", response_model=MovieCollectionResponse)
async def search_media(
    service: SearchServiceDependency,
    query: str = Query(min_length=1, max_length=100),
    page: int = Query(default=1, ge=1, le=500),
) -> MovieCollectionResponse:
    try:
        return await service.search_media(query, page)
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/media/discover", response_model=MovieCollectionResponse)
async def discover_media(
    service: SearchServiceDependency,
    media_type: MediaType,
    genre_id: int | None = Query(default=None, ge=1),
    year: int | None = Query(default=None, ge=1900, le=2100),
    sort: Literal["popular", "rating", "release"] = "popular",
    page: int = Query(default=1, ge=1, le=500),
) -> MovieCollectionResponse:
    try:
        return await service.discover_media(
            media_type=media_type,
            genre_id=genre_id,
            year=year,
            sort=sort,
            page=page,
        )
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/media/{media_type}/{tmdb_id}", response_model=MovieMetadata)
async def get_media(
    media_type: MediaType,
    tmdb_id: int,
    service: SearchServiceDependency,
) -> MovieMetadata:
    try:
        return await service.get_media(tmdb_id, media_type)
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.get("/movies/{tmdb_id}", response_model=MovieMetadata)
async def get_movie(tmdb_id: int, service: SearchServiceDependency) -> MovieMetadata:
    try:
        return await service.get_movie(tmdb_id)
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc


@router.post("/search", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    service: SearchServiceDependency,
) -> SearchResponse:
    try:
        return await service.search(
            request.tmdb_id,
            media_type=request.media_type,
            refresh=request.refresh,
            season_number=request.season_number,
        )
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc
    except SearchUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InvalidSeasonRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
