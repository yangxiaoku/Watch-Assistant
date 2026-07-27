"""Shared API and adapter data types."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, PositiveInt, SecretStr


class ResourceKind(StrEnum):
    MAGNET = "magnet"
    SHARE = "115_share"


class InspectionBatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class InspectionItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFIED = "verified"
    TIMEOUT = "timeout"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


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


class LoggingLevel(StrEnum):
    DEBUG = "DEBUG"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class LogCategory(StrEnum):
    SYSTEM = "system"
    SEARCH = "search"
    CACHE = "cache"
    INSPECTION = "inspection"
    P115 = "p115"
    SECURITY = "security"


class LoggingSettingsResponse(BaseModel):
    level: LoggingLevel
    retention_days: int = Field(ge=1, le=90)
    max_file_mb: int = Field(ge=1, le=50)
    revision: int = Field(ge=0)


class LoggingSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    level: LoggingLevel | None = None
    retention_days: int | None = Field(default=None, ge=1, le=90)
    max_file_mb: int | None = Field(default=None, ge=1, le=50)
    revision: int = Field(ge=0)


class ContentPolicyResponse(BaseModel):
    hide_adult_media: bool
    hide_suspicious_resources: bool
    hide_low_quality_resources: bool
    blocked_keywords: list[str] = Field(default_factory=list, max_length=50)
    revision: int = Field(ge=0)


class ContentPolicyPatch(BaseModel):
    model_config = {"extra": "forbid"}

    hide_adult_media: bool | None = None
    hide_suspicious_resources: bool | None = None
    hide_low_quality_resources: bool | None = None
    blocked_keywords: list[str] | None = Field(default=None, max_length=50)
    revision: int = Field(ge=0)


class InspectionSettingsResponse(BaseModel):
    auto_start_enabled: bool
    revision: int = Field(ge=0)


class InspectionSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    auto_start_enabled: bool
    revision: int = Field(ge=0)


class OrganizationPlanResponse(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str
    plan_hash: str
    status: Literal["needs_review", "planned", "invalidated", "ignored"]
    revision: int = Field(ge=1)
    expires_at: datetime
    source_count: int = Field(ge=0)
    action_count: int = Field(ge=0)
    precondition_count: int = Field(ge=0)
    alias: str | None = None


class OrganizationPlanListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[OrganizationPlanResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class OrganizationPlanMutationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)


class OrganizationOperationQueueRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


class OrganizationOperationBatchItem(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


class OrganizationOperationBatchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[OrganizationOperationBatchItem] = Field(min_length=1, max_length=20)


class OrganizationOperationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    operation_id: str
    plan_id: str
    status: Literal[
        "planned", "organizing", "organized", "failed", "uncertain", "cancelled"
    ]
    revision: int = Field(ge=1)
    attempts: int = Field(ge=0)
    error_code: str | None = None


class OrganizationOperationBatchResult(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str
    operation_id: str | None = None
    status: Literal[
        "planned",
        "organizing",
        "organized",
        "failed",
        "uncertain",
        "cancelled",
        "rejected",
    ]
    revision: int | None = Field(default=None, ge=1)
    attempts: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    message: str


class OrganizationOperationBatchResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[OrganizationOperationBatchResult]


class OrganizationPlanAliasRequest(OrganizationPlanMutationRequest):
    alias: str = Field(min_length=1, max_length=64)


class CredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}

    value: SecretStr
    revision: int = Field(ge=0)


class CredentialResetRequest(BaseModel):
    model_config = {"extra": "forbid"}

    revision: int = Field(ge=0)


class CredentialSourceResponse(BaseModel):
    configured: bool
    source: Literal["managed", "environment"]
    last_updated_at: datetime | None


class P115CredentialStatus(BaseModel):
    configured: bool
    source: Literal["managed", "tgtodrive"]
    last_updated_at: datetime | None
    structure_valid: bool
    ready: bool


class CredentialSettingsResponse(BaseModel):
    revision: int = Field(ge=0)
    tmdb: CredentialSourceResponse
    p115_cookie: P115CredentialStatus


class SettingsOverviewResponse(BaseModel):
    release: str
    uptime_seconds: int = Field(ge=0)
    database_size_bytes: int = Field(ge=0)
    capabilities: dict[str, bool]


class LogItem(BaseModel):
    id: int = Field(ge=1)
    timestamp: datetime
    level: LoggingLevel
    category: LogCategory
    message: str


class LogsResponse(BaseModel):
    items: list[LogItem]
    next_cursor: int | None = Field(default=None, ge=1)


class SubmissionResult(BaseModel):
    status: RemoteStatus
    remote_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == RemoteStatus.ACCEPTED


class SeasonMetadata(BaseModel):
    season_number: int
    name: str
    episode_count: int
    air_date: str | None = None
    poster_path: str | None = None


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
    adult: bool = False
    seasons: list[SeasonMetadata] = Field(default_factory=list)


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
    size_source: Literal["pansou", "inspection"] | None = None
    seeders_source: Literal["pansou"] | None = None
    seeders_observed_at: datetime | None = None
    rank_score: int = Field(default=0, ge=0, le=100)
    relevance_score: int = Field(default=0, ge=0, le=100)
    completeness_score: int = Field(default=0, ge=0, le=100)


class ResourcePageResponse(BaseModel):
    items: list[ResourceSummary]
    page: int = Field(ge=1)
    page_size: Literal[25, 50, 100]
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    facets: dict[str, int]
    snapshot_revision: str
    hidden_total: int = Field(default=0, ge=0)


class SearchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    tmdb_id: PositiveInt
    media_type: MediaType = MediaType.MOVIE
    season_number: int | None = Field(default=None, ge=0)
    refresh: bool = False


class SearchResponse(BaseModel):
    movie: MovieMetadata
    results: list[ResourceSummary]
    warnings: list[str] = Field(default_factory=list)
    cached: bool = False
    cache_age_seconds: int | None = None
    selected_season: int | None = Field(default=None, ge=0)
    hidden_total: int = Field(default=0, ge=0)


class MediaIdentity(BaseModel):
    model_config = {"frozen": True}

    media_type: MediaType
    tmdb_id: PositiveInt


class CacheWarmStatusResponse(BaseModel):
    running: bool
    last_started_at: datetime | None
    last_completed_at: datetime | None
    next_run_at: datetime
    total_count: int
    success_count: int
    failure_count: int
    skipped_count: int
    failed_media: list[MediaIdentity] = Field(default_factory=list)


class CacheRetryResponse(BaseModel):
    scheduled: bool
    count: int


class MovieWatchSummary(BaseModel):
    model_config = {"from_attributes": True}

    media_type: MediaType
    tmdb_id: int
    title: str
    original_title: str | None
    release_year: int | None
    active: bool
    first_empty_at: datetime
    last_checked_at: datetime
    found_at: datetime | None


class SourceReliabilitySummary(BaseModel):
    source: str
    accepted_count: int
    rejected_count: int
    link_ok_count: int
    link_bad_count: int
    penalty: int


class TaskCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_id: str = Field(min_length=1, max_length=40)
    force: bool = False


class InspectionStartRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_ids: list[str] = Field(min_length=1, max_length=30)

    def model_post_init(self, _context: Any) -> None:
        if any(
            not resource_id or len(resource_id) > 40
            for resource_id in self.resource_ids
        ):
            raise ValueError("invalid_resource_id")
        if len(set(self.resource_ids)) != len(self.resource_ids):
            raise ValueError("duplicate_resource_id")


class InspectionResultResponse(BaseModel):
    resource_id: str
    infohash: str | None
    status: InspectionItemStatus
    total_size_bytes: int
    file_count: int
    video_file_count: int
    video_size_bytes: int
    subtitle_count: int
    sample_count: int
    largest_video_name: str | None
    content_summary: str | None
    error_code: str | None


class InspectionBatchResponse(BaseModel):
    batch_id: str
    status: InspectionBatchStatus
    submitted_count: int
    completed_count: int
    results: list[InspectionResultResponse] = Field(default_factory=list)


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
