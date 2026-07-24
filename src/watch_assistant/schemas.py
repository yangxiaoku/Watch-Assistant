"""Shared API and adapter data types."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, PositiveInt


class ResourceKind(StrEnum):
    MAGNET = "magnet"
    SHARE = "115_share"


class TaskAction(StrEnum):
    OFFLINE_DOWNLOAD = "offline_download"
    SAVE_SHARE = "save_share"


class MovieMetadata(BaseModel):
    tmdb_id: int
    title: str
    original_title: str | None = None
    release_year: int | None = None
    overview: str | None = None
    poster_path: str | None = None


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
    tmdb_id: PositiveInt
    refresh: bool = False


class SearchResponse(BaseModel):
    movie: MovieMetadata
    results: list[ResourceSummary]
    warnings: list[str] = Field(default_factory=list)
    cached: bool = False
    cache_age_seconds: int | None = None
