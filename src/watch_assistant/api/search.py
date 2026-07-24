"""Movie and PanSou search routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.adapters.tmdb import TmdbError
from watch_assistant.schemas import (
    MovieCollectionResponse,
    MovieMetadata,
    SearchRequest,
    SearchResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.search import SearchService, SearchUnavailable

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


SearchServiceDependency = Annotated[SearchService, Depends(get_search_service)]


@router.get("/movies/popular", response_model=MovieCollectionResponse)
async def get_popular_movies(
    service: SearchServiceDependency,
) -> MovieCollectionResponse:
    try:
        return MovieCollectionResponse(results=await service.get_popular())
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


@router.get("/movies/{tmdb_id}", response_model=MovieMetadata)
async def get_movie(
    tmdb_id: int, service: SearchServiceDependency
) -> MovieMetadata:
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
        return await service.search(request.tmdb_id, refresh=request.refresh)
    except TmdbError as exc:
        raise HTTPException(status_code=502, detail="tmdb_unavailable") from exc
    except SearchUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
