"""Shared API and adapter data types."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, PositiveInt


class ResourceKind(StrEnum):
    MAGNET = "magnet"
    SHARE = "115_share"


class MediaType(StrEnum):
    MOVIE = "movie"
    TV = "tv"


class TaskAction(StrEnum):
    OFFLINE_DOWNLOAD = "offline_download"
    SAVE_SHARE = "save_share"


class RemoteStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SubmissionResult(BaseModel):
    status: RemoteStatus
    remote_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == RemoteStatus.ACCEPTED


class MovieMetadata(BaseModel):
    tmdb_id: int
    media_type: MediaType = MediaType.MOVIE
    title: str
    original_title: str | None = None
    release_year: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    genre_ids: list[int] = Field(default_factory=list)
    vote_average: float | None = None


class MovieCollectionResponse(BaseModel):
    results: list[MovieMetadata]
    page: int = 1
    total_pages: int = 1
    total_results: int = 0


class HomeCatalogResponse(BaseModel):
    popular: list[MovieMetadata]
    now_playing: list[MovieMetadata]
    upcoming: list[MovieMetadata]
    top_rated: list[MovieMetadata]
    tv_popular: list[MovieMetadata]
    tv_on_the_air: list[MovieMetadata]
    tv_top_rated: list[MovieMetadata]


class NormalizedResource(BaseModel):
    kind: ResourceKind
    canonical_key: str
    name: str
    url: str
    password: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    source: str
    captured_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceSummary(BaseModel):
    resource_id: str
    kind: ResourceKind
    name: str
    size_bytes: int | None
    seeders: int | None
    source: str
    captured_at: datetime


class SearchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    tmdb_id: PositiveInt
    media_type: MediaType = MediaType.MOVIE
    refresh: bool = False


class SearchResponse(BaseModel):
    movie: MovieMetadata
    results: list[ResourceSummary]
    warnings: list[str] = Field(default_factory=list)
    cached: bool = False
    cache_age_seconds: int | None = None


class TaskCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_id: str = Field(min_length=1, max_length=40)
    force: bool = False


class TaskResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    resource_id: str | None
    action: TaskAction
    state: str
    attempts: int
    remote_ref: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None


class AuthLoginRequest(BaseModel):
    model_config = {"extra": "forbid"}

    password: str = Field(min_length=1)


class AuthLoginResponse(BaseModel):
    csrf_token: str
