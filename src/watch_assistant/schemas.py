"""Shared API and adapter data types."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    Field,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)

from watch_assistant.services.source_health import SourceHealthState


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


class WorkflowStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    WAITING_USER_CONFIRMATION = "waiting_user_confirmation"
    WAITING_EXTERNAL = "waiting_external"
    PARTIAL = "partial"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RESULT_PENDING_CONFIRMATION = "result_pending_confirmation"


class WorkflowStageName(StrEnum):
    DISCOVERY = "discovery"
    INSPECTION = "inspection"
    APPROVAL = "approval"
    PUSH = "push"
    AVAILABILITY = "availability"
    ORGANIZATION = "organization"
    STRM = "strm"


class WorkflowStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_EXTERNAL = "waiting_external"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class SubscriptionMode(StrEnum):
    REMIND = "remind"
    CONFIRM = "confirm"
    AUTO = "auto"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    MATCHED = "matched"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_MATCH = "no_match"


class QualityProfileScope(StrEnum):
    GLOBAL = "global"
    LIBRARY = "library"
    SUBSCRIPTION = "subscription"
    MEDIA = "media"


class RemoteStatus(StrEnum):
    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    DOWNLOADING = "downloading"
    AVAILABLE = "available"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class EvidenceStatus(StrEnum):
    DISCOVERED = "discovered"
    SUBMITTED = "submitted"
    DOWNLOADING = "downloading"
    AVAILABLE = "available"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class EvidenceSource(StrEnum):
    RESOURCE_RECORD = "resource_record"
    SUBMISSION_RECEIPT = "submission_receipt"
    READONLY_RECONCILIATION = "readonly_reconciliation"


@dataclass(frozen=True, slots=True, repr=False)
class RemoteObservation:
    """Sanitized remote state plus the minimum file evidence for availability."""

    status: RemoteStatus
    file_id: str | None = None
    parent_id: str | None = None
    is_directory: bool | None = None
    error_code: str | None = None

    @property
    def availability_verified(self) -> bool:
        try:
            status = RemoteStatus(self.status)
        except (TypeError, ValueError):
            return False
        return bool(
            status is RemoteStatus.AVAILABLE
            and _stable_remote_id(self.file_id)
            and _stable_remote_id(self.parent_id)
            and self.is_directory is False
            and self.error_code is None
        )

    def __repr__(self) -> str:
        try:
            status = RemoteStatus(self.status).value
        except (TypeError, ValueError):
            status = "unknown"
        return (
            "RemoteObservation(status="
            f"{status!r}, availability_verified="
            f"{self.availability_verified!r}, error_code={self.error_code!r})"
        )


def _stable_remote_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    if not normalized.isdigit():
        return None
    # "0" is the 115 root directory, a legal parent for availability evidence.
    if normalized == "0":
        return normalized
    if normalized.startswith("0"):
        return None
    return normalized


class LoggingLevel(StrEnum):
    DEBUG = "DEBUG"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class LogCategory(StrEnum):
    SYSTEM = "system"
    SECURITY = "security"
    SEARCH = "search"
    PANSOU = "pansou"
    CACHE = "cache"
    INSPECTION = "inspection"
    P115 = "p115"
    TASK = "task"
    ORGANIZE = "organize"
    STRM = "strm"
    LIBRARY = "library"
    AGENT = "agent"
    SETTINGS = "settings"
    SUBSCRIPTION = "subscription"
    QUALITY = "quality"
    NOTIFICATION = "notification"
    BUSINESS = "business"


class NotificationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
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


class LibraryScanSummary(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: str
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    complete: bool
    snapshot_revision: int | None = Field(default=None, ge=0)
    pages_read: int = Field(ge=0)
    items_seen: int = Field(ge=0)
    added_count: int = Field(ge=0)
    changed_count: int = Field(ge=0)
    removed_count: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    state_message_zh: str = ""
    error_code: str | None = None
    error_message_zh: str | None = None
    cancel_requested: bool = False


class MediaLibraryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    name: str
    root_directory_id: str
    enabled: bool
    scope_verified: bool
    revision: int = Field(ge=0)
    latest_scan: LibraryScanSummary | None = None


class MediaLibraryConfigurationPatch(BaseModel):
    """Admin-only declaration of the one 115 root covered by inventory."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    root_directory_id: str = Field(pattern=r"^[1-9][0-9]{0,127}$")
    revision: int = Field(ge=0)


class MediaLibraryVerificationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library: MediaLibraryResponse
    verified: bool
    enabled: bool


class LibraryScanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    idempotency_key: str = Field(min_length=1, max_length=128)
    max_directories: int = Field(default=10_000, ge=1, le=100_000)


class OrganizationPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)
    source_directory_id: str | None = Field(default=None, min_length=1, max_length=128)


class MediaLibraryListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[MediaLibraryResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class LibraryScanListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[LibraryScanSummary]
    next_cursor: int | None = Field(default=None, ge=0)


class MediaEntryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    media_id: str
    library_id: str
    scan_run_id: str
    object_type: Literal["file"]
    object_id: str
    parent_id: str | None = None
    name: str
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None
    state: Literal["indexed"] = "indexed"


class MediaEntryListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[MediaEntryResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class StrmGenerationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)


class StrmManifestItemResponse(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_id: str
    library_id: str
    cloud_file_id: str
    cloud_relative_path: str
    local_relative_path: str
    status: Literal["pending", "verified", "retired"]
    source_version: int = Field(ge=0)


class StrmManifestListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[StrmManifestItemResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class StrmGenerationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    operation_id: str
    library_id: str
    scan_run_id: str
    generated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    retired: int = Field(default=0, ge=0)


class StrmOperationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    operation_id: str
    library_id: str
    source_scan_run_id: str
    workflow_id: str | None = None
    kind: Literal["full", "incremental", "cleanup", "small_file_cleanup"]
    status: Literal[
        "queued", "running", "succeeded", "failed", "timeout", "cancelled"
    ]
    generated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    retired: int = Field(ge=0)
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StrmOperationListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[StrmOperationResponse]
    next_cursor: str | None = None


class StrmCleanupPlanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)


class StrmCleanupPlanResponse(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str
    library_id: str
    source_scan_run_id: str
    source_snapshot_revision: int = Field(ge=0)
    plan_hash: str = Field(min_length=64, max_length=64)
    status: Literal["needs_review", "invalidated", "applied"]
    revision: int = Field(ge=1)
    expires_at: datetime
    candidate_count: int = Field(ge=0)
    executable_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)


class StrmCleanupPlanApplyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )
    confirm: bool = False
    idempotency_key: str = Field(min_length=1, max_length=128)


class StrmCleanupPlanApplyResponse(BaseModel):
    model_config = {"extra": "forbid"}

    plan: StrmCleanupPlanResponse
    retired: int = Field(ge=0)


class EmptyDirectoryCleanupPlanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)


class EmptyDirectoryCleanupCandidateResponse(BaseModel):
    model_config = {"extra": "forbid"}

    directory_id: str
    parent_id: str
    name: str
    path: str
    state: Literal["ready", "blocked"]


class EmptyDirectoryCleanupPlanResponse(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str
    library_id: str
    source_scan_run_id: str
    source_snapshot_revision: int = Field(ge=0)
    plan_hash: str = Field(min_length=64, max_length=64)
    status: Literal["needs_review", "applying", "invalidated", "applied"]
    revision: int = Field(ge=1)
    expires_at: datetime
    candidate_count: int = Field(ge=0)
    executable_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    candidates: list[EmptyDirectoryCleanupCandidateResponse]


class EmptyDirectoryCleanupPlanApplyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )
    confirm: bool = False
    idempotency_key: str = Field(min_length=1, max_length=128)


class EmptyDirectoryCleanupPlanApplyResponse(BaseModel):
    model_config = {"extra": "forbid"}

    plan: EmptyDirectoryCleanupPlanResponse
    deleted: int = Field(ge=0)


class SmallFileCleanupCandidateResponse(BaseModel):
    model_config = {"extra": "forbid"}

    file_id: str
    parent_id: str
    name: str
    size_bytes: int


class SmallFileCleanupPreviewResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    source_scan_run_id: str
    snapshot_revision: int = Field(ge=0)
    threshold_bytes: int = Field(gt=0)
    candidate_count: int = Field(ge=0)
    candidates: list[SmallFileCleanupCandidateResponse]


class SmallFileCleanupApplyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)
    file_ids: list[str] = Field(default_factory=list, max_length=200)
    confirm: bool = False


class SmallFileCleanupApplyResponse(BaseModel):
    model_config = {"extra": "forbid"}

    operation_id: str
    status: Literal["running"]


class StrmVerifyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_scan_run_id: str = Field(min_length=1, max_length=128)


class StrmVerifyIssueResponse(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str
    object_id: str
    manifest_id: str | None = None


class StrmVerifyResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    scan_run_id: str
    snapshot_revision: int = Field(ge=0)
    status: Literal["verified", "issues"]
    checked_count: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    orphan_count: int = Field(ge=0)
    path_mismatch_count: int = Field(ge=0)
    issues: list[StrmVerifyIssueResponse]


class LibraryDeleteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_name: str = Field(min_length=1, max_length=255)
    confirm: bool


class LibraryDeleteResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["success", "failed", "uncertain"]
    error_code: str | None = None


class InventoryFreshnessResponse(BaseModel):
    model_config = {"extra": "forbid"}

    complete: bool
    captured_at: datetime | None = None
    age_seconds: int | None = Field(default=None, ge=0)
    threshold_seconds: int = Field(ge=60, le=86_400)
    status: Literal["fresh", "stale", "incomplete", "unknown"]


class InventoryDuplicateGroupResponse(BaseModel):
    model_config = {"extra": "forbid"}

    group_key: str
    kind: Literal[
        "exact_duplicate",
        "media_duplicate",
        "version_duplicate",
        "review_candidate",
    ]
    identity_key: str
    object_ids: list[str]
    confidence: Literal["trusted", "candidate"]


class LibraryInventoryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    scan_run_id: str | None = None
    snapshot_revision: int | None = Field(default=None, ge=0)
    freshness: InventoryFreshnessResponse
    file_count: int = Field(ge=0)
    movie_candidate_count: int = Field(ge=0)
    tv_candidate_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    duplicate_groups: list[InventoryDuplicateGroupResponse] = Field(default_factory=list)


class InventorySearchItemResponse(BaseModel):
    """INV-008: 一条库存文件搜索结果,不含敏感路径/摘要细节。"""

    model_config = {"extra": "forbid"}

    object_id: str
    name: str
    size_bytes: int | None = Field(default=None, ge=0)
    media_type: Literal["movie", "tv", "unknown"] = "unknown"
    tmdb_id: int | None = Field(default=None, ge=1)
    season: int | None = Field(default=None, ge=0)
    episode_start: int | None = Field(default=None, ge=0)
    episode_end: int | None = Field(default=None, ge=0)
    in_duplicate_group: bool = False


class InventorySearchResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    scan_run_id: str | None = None
    snapshot_revision: int | None = Field(default=None, ge=0)
    freshness: InventoryFreshnessResponse
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    items: list[InventorySearchItemResponse] = Field(default_factory=list)


class InventoryCheckResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    scan_run_id: str | None = None
    freshness: InventoryFreshnessResponse
    decision: Literal[
        "not_found",
        "exact_duplicate",
        "media_duplicate",
        "version_duplicate",
        "needs_review",
        "index_incomplete",
    ]
    matched_object_count: int = Field(ge=0)


class InventoryEventResponse(BaseModel):
    model_config = {"extra": "forbid"}

    event_id: str
    library_id: str
    scan_run_id: str | None = None
    object_type: Literal["file", "directory"]
    object_id: str
    event_kind: Literal["added", "changed", "removed", "restored"]
    previous_status: Literal["active", "missing", "quarantined", "restored"] | None = None
    created_at: datetime


class InventoryEventListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[InventoryEventResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class InventoryIdentityPatch(BaseModel):
    model_config = {"extra": "forbid"}

    tmdb_id: PositiveInt
    media_type: Literal["movie", "tv"]
    season: int | None = Field(default=None, ge=0)
    episode_start: int | None = Field(default=None, ge=1)
    episode_end: int | None = Field(default=None, ge=1)
    revision: int = Field(ge=0)

    def model_post_init(self, _context: Any) -> None:
        if self.media_type == "movie" and any(
            value is not None
            for value in (self.season, self.episode_start, self.episode_end)
        ):
            raise ValueError("movie_cannot_have_episode_scope")
        if (
            self.episode_start is not None
            and self.episode_end is not None
            and self.episode_end < self.episode_start
        ):
            raise ValueError("invalid_episode_range")


class InventoryIdentityResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    object_id: str
    tmdb_id: PositiveInt
    media_type: Literal["movie", "tv"]
    season: int | None = Field(default=None, ge=0)
    episode_start: int | None = Field(default=None, ge=1)
    episode_end: int | None = Field(default=None, ge=1)
    confidence: Literal["trusted", "candidate"]
    revision: int = Field(ge=1)


class AuditRecordResponse(BaseModel):
    model_config = {"extra": "forbid"}

    audit_id: str
    timestamp: datetime
    event_code: str
    event_version: int = Field(ge=1)
    title_zh: str
    message_zh: str
    suggestion_zh: str | None = None
    status: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    actor_type: str | None = None
    resource_type: str | None = None
    task_id: str | None = None


class AuditRecordListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[AuditRecordResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class InspectionSettingsResponse(BaseModel):
    auto_start_enabled: bool
    revision: int = Field(ge=0)


class InspectionSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    auto_start_enabled: bool
    revision: int = Field(ge=0)


class OrganizationSettingsResponse(BaseModel):
    model_config = {"extra": "forbid"}

    schedule_enabled: bool
    auto_execute_enabled: bool
    scan_interval_minutes: int = Field(ge=5, le=1440)
    source_directory_ids: list[str] = Field(max_length=50)
    source_directory_labels: list[str] = Field(default_factory=list, max_length=50)
    target_directory_id: str | None = None
    target_directory_label: str | None = None
    push_directory_id: str | None = None
    push_directory_label: str | None = None
    video_extensions: list[str] = Field(max_length=50)
    metadata_extensions: list[str] = Field(max_length=50)
    rename_enabled: bool
    media_probe_enabled: bool
    ai_identification_enabled: bool
    small_file_threshold_mb: float = Field(ge=0, le=10_240)
    cleanup_empty_directories: bool
    strm_linkage_enabled: bool
    operation_delay_seconds: float = Field(ge=0, le=60)
    include_children_category: bool
    include_concert_category: bool
    region_grouping_enabled: bool
    year_grouping_enabled: bool
    prefer_remux: bool
    prefer_resolution: bool
    prefer_dolby: bool
    conflict_mode: Literal[0, 1, 2]
    multi_version_enabled: bool
    revision: int = Field(ge=0)


class OrganizationSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    schedule_enabled: bool | None = None
    auto_execute_enabled: bool | None = None
    scan_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    source_directory_ids: list[str] | None = Field(default=None, max_length=50)
    source_directory_labels: list[str] | None = Field(default=None, max_length=50)
    target_directory_id: str | None = None
    target_directory_label: str | None = None
    push_directory_id: str | None = None
    push_directory_label: str | None = None
    video_extensions: list[str] | None = Field(default=None, max_length=50)
    metadata_extensions: list[str] | None = Field(default=None, max_length=50)
    rename_enabled: bool | None = None
    media_probe_enabled: bool | None = None
    ai_identification_enabled: bool | None = None
    small_file_threshold_mb: float | None = Field(default=None, ge=0, le=10_240)
    cleanup_empty_directories: bool | None = None
    strm_linkage_enabled: bool | None = None
    operation_delay_seconds: float | None = Field(default=None, ge=0, le=60)
    include_children_category: bool | None = None
    include_concert_category: bool | None = None
    region_grouping_enabled: bool | None = None
    year_grouping_enabled: bool | None = None
    prefer_remux: bool | None = None
    prefer_resolution: bool | None = None
    prefer_dolby: bool | None = None
    conflict_mode: Literal[0, 1, 2] | None = None
    multi_version_enabled: bool | None = None
    revision: int = Field(ge=0)


class P115CheckInSettingsResponse(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool
    check_in_time: str


class P115CheckInSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool
    check_in_time: str = Field(
        default="00:05", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )


class P115CheckInStatusResponse(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool
    is_sign_today: bool
    continuous_day: int = 0
    points_num: str = ""
    error_code: str | None = None


class OrganizationScheduleActionResponse(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["run_now", "stop"]
    queued: bool
    schedule_enabled: bool
    message_zh: str
    run_id: str | None = None


class OrganizationBlockedDetailResponse(BaseModel):
    model_config = {"extra": "forbid"}

    source_directory_id: str | None = None
    phase: Literal["credentials", "directory_read", "scan", "tmdb", "plan", "execution"]
    error_code: str
    message_zh: str
    next_step_zh: str


class OrganizationResultItemResponse(BaseModel):
    model_config = {"extra": "forbid"}

    title: str
    tmdb_id: int | None = None
    target: str | None = None
    status: Literal[
        "queued", "organizing", "success", "failed", "uncertain", "needs_review", "skipped"
    ]
    error_code: str | None = None


class OrganizationAutomationResultResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal[
        "unknown", "success", "skipped", "deleted", "replace", "failed"
    ]
    available_statuses: list[
        Literal["unknown", "success", "skipped", "deleted", "replace", "failed"]
    ]
    source_count: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    plan_count: int = Field(ge=0)
    queued_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    blocked_details: list[OrganizationBlockedDetailResponse] = Field(default_factory=list)
    items: list[OrganizationResultItemResponse] = Field(default_factory=list)
    finished_at: datetime | None = None
    run_id: str | None = None
    cleaned_small_files: int = Field(ge=0, default=0)
    cleaned_empty_dirs: int = Field(ge=0, default=0)


class OrganizationHistoryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    operation_id: str
    plan_id: str
    source_object_id: str
    source_directory_id: str
    target_directory_id: str
    tmdb_id: int | None = None
    title: str
    media_type: Literal["movie", "tv"] | None = None
    source_name: str
    target_path: str
    status: Literal["organized", "failed", "uncertain"]
    error_code: str | None = None
    completed_at: datetime


class OrganizationHistoryListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[OrganizationHistoryResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class OrganizationPlanCandidateResponse(BaseModel):
    model_config = {"extra": "forbid"}

    source_object_id: str
    tmdb_id: int
    title: str
    media_type: Literal["movie", "tv"]
    release_year: int | None = None


class OrganizationExecutionBlockerResponse(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal[
        "expired", "status", "prerequisite", "snapshot_changed", "review_only"
    ]
    code: str
    message_zh: str
    next_step_zh: str


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
    executable_action_count: int = Field(ge=0)
    review_action_count: int = Field(ge=0)
    source_names: list[str] = Field(default_factory=list)
    can_execute: bool
    execution_blockers: list[OrganizationExecutionBlockerResponse] = Field(
        default_factory=list
    )
    requires_web_approval: bool = False
    high_risk_action_threshold: int = Field(ge=1)
    alias: str | None = None
    candidates: list[OrganizationPlanCandidateResponse] = Field(default_factory=list)


class OrganizationApprovalWorkflowRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)


class OrganizationPlanListResponse(BaseModel):
    model_config = {"extra": "forbid"}

    items: list[OrganizationPlanResponse]
    next_cursor: int | None = Field(default=None, ge=0)


class OrganizationPlanMutationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    plan_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )


class OrganizationPlanCandidateRequest(OrganizationPlanMutationRequest):
    source_object_id: str = Field(min_length=1, max_length=128)
    tmdb_id: int = Field(gt=0)


class OrganizationPlanCandidateSearchRequest(OrganizationPlanMutationRequest):
    source_object_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_index: int | None = Field(default=None, ge=0, le=100)
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


class OrganizationOperationQueueRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    confirm: bool = False
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)


class OrganizationOperationBatchItem(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    confirm: bool = False
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)


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
    cancel_requested: bool = False
    workflow_id: str | None = None


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
    workflow_id: str | None = None


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
    source: Literal["managed", "file"]
    last_updated_at: datetime | None
    structure_valid: bool
    ready: bool


class CredentialSettingsResponse(BaseModel):
    revision: int = Field(ge=0)
    tmdb: CredentialSourceResponse
    p115_cookie: P115CredentialStatus


class ProwlarrSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = None
    revision: int = Field(ge=0)


class ProwlarrSettingsResponse(BaseModel):
    source: Literal["managed", "environment", "none"]
    enabled: bool
    configured: bool
    base_url: str | None
    api_key_configured: bool
    api_key_source: Literal["managed", "environment", "none"]
    last_updated_at: datetime | None
    revision: int = Field(ge=0)
    health_state: SourceHealthState | None = None
    health_message_code: str | None = None
    health_message_zh: str | None = None
    health_reason_code: str | None = None
    health_reason_zh: str | None = None
    health_checked_at: datetime | None = None
    health_retry_after_seconds: int | None = Field(default=None, ge=0)
    health_consecutive_failures: int = Field(default=0, ge=0)


class ProwlarrVerifyResponse(BaseModel):
    source: Literal["prowlarr"] = "prowlarr"
    status: Literal["available", "unavailable", "disabled"]
    configured: bool
    base_url: str | None
    message_code: str | None = None
    checked_at: datetime
    state: SourceHealthState | None = None
    message_zh: str | None = None
    reason_code: str | None = None
    reason_zh: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)


class SearchSourceStateResponse(BaseModel):
    enabled: bool
    configured: bool
    status: Literal["disabled", "configured", "unavailable"]
    message_code: str | None = None
    state: SourceHealthState | None = None
    message_zh: str | None = None
    reason_code: str | None = None
    reason_zh: str | None = None
    checked_at: datetime | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)


class SearchSourcesResponse(BaseModel):
    pansou: SearchSourceStateResponse
    prowlarr: SearchSourceStateResponse


class CapabilityAvailabilityResponse(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool
    reason_code: str | None = None
    reason_zh: str
    settings_section: Literal["overview", "organization"] | None = None


class SettingsOverviewResponse(BaseModel):
    release: str
    uptime_seconds: int = Field(ge=0)
    database_size_bytes: int = Field(ge=0)
    capabilities: dict[str, bool]
    capability_statuses: dict[str, "CapabilityStatusResponse"] = Field(
        default_factory=dict
    )
    capability_details: dict[str, CapabilityAvailabilityResponse] = Field(
        default_factory=dict
    )


class CapabilityState(StrEnum):
    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    CONTRACT_VERIFIED = "contract_verified"
    RUNTIME_HEALTHY = "runtime_healthy"
    RECENT_SUCCESS = "recent_success"


class CapabilityStatusResponse(BaseModel):
    state: CapabilityState
    state_zh: str
    last_success_at: datetime | None = None


class CompatibilityStatus(StrEnum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class DeploymentComponentResponse(BaseModel):
    key: str
    name_zh: str
    status: CompatibilityStatus
    version: str | None = None
    expected: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    message_zh: str
    suggestion_zh: str | None = None


class DeploymentDiagnosticsResponse(BaseModel):
    release: str
    generated_at: datetime
    overall_status: CompatibilityStatus
    write_safe: bool
    components: list[DeploymentComponentResponse]
    applied_migrations: list[str] = Field(default_factory=list)
    pending_migrations: list[str] = Field(default_factory=list)
    database_integrity: CompatibilityStatus


class SubtitleLanguage(StrEnum):
    ZH_HANS = "zh-Hans"
    ZH_HANT = "zh-Hant"
    ZH = "zh"
    EN = "en"
    OTHER = "other"
    UNKNOWN = "unknown"


class SubtitleAnalyzeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    video_name: str = Field(min_length=1, max_length=255)
    subtitle_names: list[str] = Field(min_length=1, max_length=100)


class SubtitleCandidateResponse(BaseModel):
    name: str
    extension: str
    language: SubtitleLanguage
    forced: bool
    sdh: bool
    match_status: Literal["matched", "conflict", "unmatched", "unsupported"]
    confidence: Literal["high", "medium", "low", "none"]
    recommended_name: str | None = None


class SubtitleAnalyzeResponse(BaseModel):
    video_name: str
    items: list[SubtitleCandidateResponse]
    warnings: list[str] = Field(default_factory=list)


class LogItem(BaseModel):
    id: int = Field(ge=1)
    timestamp: datetime
    level: LoggingLevel
    category: LogCategory
    message: str
    event_code: str = "legacy.log"
    event_version: int = Field(default=1, ge=1)
    title_zh: str = "应用日志"
    message_zh: str
    suggestion_zh: str | None = None
    status: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    task_id: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    error_code: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class LogsResponse(BaseModel):
    items: list[LogItem]
    next_cursor: int | None = Field(default=None, ge=1)


class NotificationResponse(BaseModel):
    id: str
    event_code: str
    severity: NotificationSeverity
    title_zh: str
    message_zh: str
    action_type: str | None
    action_id: str | None
    aggregate_count: int
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class NotificationPreferencePatch(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool | None = None
    muted_event_codes: list[str] | None = Field(default=None, max_length=100)
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    quiet_hours_timezone: str | None = None
    error_bypass_quiet_hours: bool | None = None
    revision: int = Field(ge=1)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_quiet_hours_clock(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
        except (ValueError, TypeError):
            raise ValueError("静默时段必须使用 HH:MM 格式") from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59) or len(value) != 5:
            raise ValueError("静默时段必须使用 HH:MM 格式")
        return value

    @field_validator("quiet_hours_timezone")
    @classmethod
    def validate_quiet_hours_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("静默时区无效") from None
        return value


class NotificationPreferenceResponse(BaseModel):
    enabled: bool
    muted_event_codes: list[str]
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    quiet_hours_timezone: str
    error_bypass_quiet_hours: bool
    revision: int


class BackupResponse(BaseModel):
    backup_id: str
    created_at: datetime
    file_name: str
    size_bytes: int = Field(ge=0)
    sha256: str
    schema_migrations: list[str] = Field(default_factory=list)
    release: str


class BackupListResponse(BaseModel):
    items: list[BackupResponse]


class BackupRestorePreviewResponse(BaseModel):
    backup_id: str
    status: Literal["ready", "invalid", "incompatible"]
    sha256_valid: bool
    integrity_ok: bool
    migration_compatible: bool
    backup_release: str
    current_release: str
    missing_migrations: list[str] = Field(default_factory=list)
    requires_reconfiguration: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ManualImportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    url: str = Field(min_length=1, max_length=4096)
    password: SecretStr | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    tmdb_id: PositiveInt
    media_type: MediaType = MediaType.MOVIE
    season_number: int | None = Field(default=None, ge=0)
    confirmed: bool = False


class ManualImportPreviewResponse(BaseModel):
    status: Literal["ready", "duplicate", "invalid", "media_mismatch"]
    kind: ResourceKind | None = None
    name: str | None = None
    source: str | None = None
    resource_id: str | None = None
    tmdb_id: int
    media_type: MediaType
    warnings: list[str] = Field(default_factory=list)


class ManualImportResponse(BaseModel):
    status: Literal["created", "duplicate"]
    resource_id: str
    kind: ResourceKind
    name: str
    task_created: bool = False


class SubmissionResult(BaseModel):
    status: RemoteStatus
    remote_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status in {RemoteStatus.ACCEPTED, RemoteStatus.SUBMITTED}


class SeasonMetadata(BaseModel):
    season_number: int
    name: str
    episode_count: int
    air_date: str | None = None
    poster_path: str | None = None
    tmdb_season_id: int | None = None
    overview_available: bool = False


class SeasonEpisodeMetadata(BaseModel):
    episode_number: int = Field(ge=0)
    name: str
    overview: str | None = None
    air_date: str | None = None
    still_path: str | None = None
    runtime: int | None = Field(default=None, ge=0)
    vote_average: float | None = None


class SeasonDetailResponse(BaseModel):
    series_tmdb_id: int = Field(ge=1)
    tmdb_season_id: int | None = Field(default=None, ge=1)
    season_number: int = Field(ge=0)
    name: str
    overview: str | None = None
    overview_language: str | None = None
    poster_path: str | None = None
    air_date: str | None = None
    episode_count: int = Field(ge=0)
    vote_average: float | None = None
    source: Literal["tmdb"] = "tmdb"
    fetched_at: datetime
    cached: bool = False
    stale: bool = False
    data_version: int = Field(default=1, ge=1)
    episodes: list[SeasonEpisodeMetadata] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EpisodeCompletenessItemResponse(BaseModel):
    model_config = {"extra": "forbid"}

    episode_number: int = Field(ge=1)
    status: Literal[
        "owned",
        "multiple",
        "missing",
        "unaired",
        "unknown",
        "ignored",
        "special",
    ]
    file_ids: list[str] = Field(default_factory=list)


class EpisodeCompletenessResponse(BaseModel):
    model_config = {"extra": "forbid"}

    library_id: str
    series_tmdb_id: int = Field(ge=1)
    season_number: int = Field(ge=0)
    season: SeasonDetailResponse
    freshness: InventoryFreshnessResponse
    inventory_complete: bool
    conclusion_available: bool
    items: list[EpisodeCompletenessItemResponse] = Field(default_factory=list)
    unrecognized_file_ids: list[str] = Field(default_factory=list)
    missing_episodes: list[int] = Field(default_factory=list)
    duplicate_episodes: list[int] = Field(default_factory=list)


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
    sources: list[str] = Field(default_factory=list)
    source_count: int = Field(default=0, ge=0)
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


class ResourceSearchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    season_number: int | None = Field(default=None, ge=0)
    refresh: bool = False
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)


class ResourceSearchResponse(BaseModel):
    task_id: str
    workflow_id: str | None = None
    tmdb_id: int = Field(ge=1)
    media_type: MediaType
    season_number: int | None = Field(default=None, ge=0)
    status: Literal["queued", "running", "ready", "failed"]
    snapshot_revision: str | None = None
    query_plan_version: str = "v5"
    cache_age_seconds: int | None = Field(default=None, ge=0)
    sources: list[str] = Field(default_factory=list)
    selected_season: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class MediaDetailMetricRequest(BaseModel):
    model_config = {"extra": "forbid"}

    stage: Literal[
        "detail_framework",
        "metadata_summary",
        "metadata_complete",
        "metadata_failed",
        "resource_first_batch",
        "resource_complete",
        "resource_failed",
        "late_response",
        "request_cancelled",
    ]
    status: Literal["success", "failed", "discarded", "cancelled"]
    duration_ms: int = Field(ge=0, le=3_600_000)
    cached: bool = False
    season_number: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{1,63}$")


class MediaDetailMetricResponse(BaseModel):
    accepted: bool = True


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
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)
    target_directory_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$"
    )


class InspectionStartRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_ids: list[str] = Field(min_length=1, max_length=30)
    force: bool = False
    workflow_id: str | None = Field(default=None, min_length=1, max_length=40)

    def model_post_init(self, _context: Any) -> None:
        if any(
            not resource_id or len(resource_id) > 40
            for resource_id in self.resource_ids
        ):
            raise ValueError("invalid_resource_id")
        if len(set(self.resource_ids)) != len(self.resource_ids):
            raise ValueError("duplicate_resource_id")


class SubscriptionCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    tmdb_id: PositiveInt
    media_type: MediaType = MediaType.MOVIE
    season_number: int | None = Field(default=None, ge=0)
    episode_start: int | None = Field(default=None, ge=1)
    episode_end: int | None = Field(default=None, ge=1)
    mode: SubscriptionMode = SubscriptionMode.REMIND
    quality_profile_id: str | None = Field(default=None, min_length=1, max_length=64)

    def model_post_init(self, _context: Any) -> None:
        if (
            self.episode_start is not None
            and self.episode_end is not None
            and self.episode_end < self.episode_start
        ):
            raise ValueError("invalid_episode_range")
        if self.media_type == MediaType.MOVIE and (
            self.season_number is not None
            or self.episode_start is not None
            or self.episode_end is not None
        ):
            raise ValueError("movie_cannot_have_episode_scope")


class SubscriptionMutationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    revision: int = Field(ge=1)


class SubscriptionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    tmdb_id: int
    media_type: MediaType
    season_number: int | None
    episode_start: int | None
    episode_end: int | None
    mode: SubscriptionMode
    status: SubscriptionStatus
    quality_profile_id: str | None
    next_check_at: datetime | None
    last_checked_at: datetime | None
    last_match_count: int
    last_error_code: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class SubscriptionCheckResponse(BaseModel):
    subscription: SubscriptionResponse
    matched_count: int = Field(ge=0)
    resource_ids: list[str] = Field(default_factory=list)
    new_resource_ids: list[str] = Field(default_factory=list)


class SubscriptionResourceObservationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    resource_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int = Field(ge=1)


class QualityProfileCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    scope: QualityProfileScope = QualityProfileScope.GLOBAL
    scope_key: str | None = Field(default=None, max_length=128)
    rules: dict[str, Any] = Field(default_factory=dict)


class QualityProfilePatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=64)
    rules: dict[str, Any] | None = None
    revision: int = Field(ge=1)


class QualityProfileResponse(BaseModel):
    id: str
    name: str
    scope: QualityProfileScope
    scope_key: str | None
    rules: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class QualitySimulationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_ids: list[str] = Field(min_length=1, max_length=30)


class QualitySimulationItem(BaseModel):
    resource_id: str
    eligible: bool
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


class QualitySimulationResponse(BaseModel):
    profile: QualityProfileResponse
    items: list[QualitySimulationItem]


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
    result_source: Literal["new_torrent", "existing_torrent"] | None = None


class InspectionBatchResponse(BaseModel):
    batch_id: str
    workflow_id: str | None = None
    status: InspectionBatchStatus
    submitted_count: int
    completed_count: int
    results: list[InspectionResultResponse] = Field(default_factory=list)


class WorkflowCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    media_type: MediaType | None = None
    tmdb_id: PositiveInt | None = None
    subscription_id: str | None = Field(default=None, max_length=64)
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)


class WorkflowDiscoveryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_id: str = Field(min_length=1, max_length=128)


class WorkflowStagePatch(BaseModel):
    model_config = {"extra": "forbid"}

    status: WorkflowStageStatus
    reason: str | None = Field(default=None, max_length=255)
    error_code: str | None = Field(default=None, max_length=100)
    child_type: str | None = Field(default=None, max_length=64)
    child_id: str | None = Field(default=None, max_length=128)


class WorkflowApprovalRequest(BaseModel):
    model_config = {"extra": "forbid"}

    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=255)


class WorkflowCancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str | None = Field(default=None, max_length=255)


class WorkflowStageResponse(BaseModel):
    id: str
    stage: WorkflowStageName
    sequence: int
    status: WorkflowStageStatus
    status_zh: str
    reason: str | None
    reason_zh: str | None
    error_code: str | None
    child_type: str | None
    child_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class WorkflowResponse(BaseModel):
    id: str
    correlation_id: str
    media_type: MediaType | None
    tmdb_id: int | None
    subscription_id: str | None
    status: WorkflowStatus
    status_zh: str
    state_reason: str | None
    state_reason_zh: str | None
    created_at: datetime
    updated_at: datetime
    stages: list[WorkflowStageResponse] = Field(default_factory=list)


class WorkflowListResponse(BaseModel):
    items: list[WorkflowResponse]
    page: int
    page_size: int
    total: int


class WorkflowStatsResponse(BaseModel):
    """FLOW-008: 顶层 workflow 聚合统计,不含任何敏感链接。"""

    model_config = {"extra": "forbid"}

    total: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_media_type: dict[str, int] = Field(default_factory=dict)
    recent_created_24h: int = Field(ge=0)
    recent_created_7d: int = Field(ge=0)
    active: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=100)


class WorkflowEvidenceResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    workflow_id: str | None
    task_id: str | None
    stage: WorkflowStageName | None
    evidence_type: str
    source: EvidenceSource
    subject_id: str
    status: EvidenceStatus
    verified: bool
    observed_at: datetime


class WorkflowEvidenceListResponse(BaseModel):
    items: list[WorkflowEvidenceResponse] = Field(default_factory=list)


class TaskResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    resource_id: str | None
    workflow_id: str | None
    target_directory_id: str | None
    action: TaskAction
    state: str
    attempts: int
    remote_ref: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    state_zh: str = ""
    state_reason_zh: str | None = None

    @model_validator(mode="after")
    def add_chinese_state(self) -> "TaskResponse":
        normalized_state = "submitted" if self.state == "accepted" else self.state
        self.state = normalized_state
        self.state_zh = {
            "queued": "排队中",
            "submitting": "提交中",
            "submitted": "已受理，等待文件可用",
            "downloading": "下载中，等待文件可用",
            "available": "文件已可用",
            "needs_auth": "需要重新登录",
            "failed": "处理失败",
            "uncertain": "结果待确认",
            "cancelled": "已取消",
        }.get(normalized_state, "状态待确认")
        self.state_reason_zh = {
            "submitted": "115 已受理，尚未取得文件可用证据。",
            "downloading": "115 正在下载，尚未取得文件可用证据。",
            "available": "已取得只读文件可用证据。",
            "uncertain": "远端结果暂时无法确认，请先核对，不要重复提交。",
        }.get(normalized_state)
        return self


class TaskReconciliationResponse(BaseModel):
    task: TaskResponse
    evidence: WorkflowEvidenceResponse


class TaskBatchRetryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    limit: int = Field(default=50, ge=1, le=200)


class TaskBatchFailure(BaseModel):
    task_id: str
    code: str


class TaskBatchRetryResponse(BaseModel):
    requested: int
    retried: int
    failed: list[TaskBatchFailure]


class WorkflowBatchCancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    limit: int = Field(default=50, ge=1, le=200)


class WorkflowBatchFailure(BaseModel):
    workflow_id: str
    code: str


class WorkflowBatchCancelResponse(BaseModel):
    requested: int
    cancelled: int
    failed: list[WorkflowBatchFailure]


class AuthLoginRequest(BaseModel):
    model_config = {"extra": "forbid"}

    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(min_length=1)


class AuthLoginResponse(BaseModel):
    csrf_token: str


class AgentTokenCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=list, max_length=32)
    library_ids: list[str] = Field(default_factory=list, max_length=100)
    expires_at: datetime | None = None


class AgentTokenResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    library_ids: list[str]
    expires_at: datetime | None
    paused_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None
    last_client_version: str | None
    call_count: int = Field(ge=0)
    status: Literal["active", "paused", "revoked", "expired"]


class AgentTokenCreateResponse(BaseModel):
    token: str
    item: AgentTokenResponse


class AgentTokenListResponse(BaseModel):
    items: list[AgentTokenResponse]


class AgentCapabilitiesResponse(BaseModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["v1"] = "v1"
    token_id: str | None
    token_name: str | None
    scopes: list[str]
    library_ids: list[str]
    capabilities: dict[str, bool]


class WebhookEndpointCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=8, max_length=2048)
    event_codes: list[str] = Field(default_factory=list, max_length=100)


class WebhookEndpointResponse(BaseModel):
    id: str
    name: str
    url: str
    secret_prefix: str
    event_codes: list[str]
    enabled: bool
    health_status: Literal["disabled", "unknown", "healthy", "degraded", "failed"]
    revision: int
    created_at: datetime
    updated_at: datetime
    last_success_at: datetime | None
    last_failure_at: datetime | None
    failure_count: int
    delivery_total: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    next_retry_at: datetime | None


class WebhookEndpointCreateResponse(BaseModel):
    secret: str
    item: WebhookEndpointResponse


class WebhookEndpointListResponse(BaseModel):
    items: list[WebhookEndpointResponse]


class WebhookEndpointPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    event_codes: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    revision: int = Field(ge=1)


class WebhookDeliveryResponse(BaseModel):
    id: str
    endpoint_id: str
    event_id: str
    event_code: str
    status: Literal["pending", "delivered", "dead"]
    attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error_code: str | None
    last_response_summary: str | None
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]


class NotifyChannelCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["feishu", "feishu_cli", "clawbot"] = "feishu"
    webhook_url: str | None = Field(default=None, min_length=8, max_length=2048)
    target: str | None = Field(default=None, min_length=1, max_length=128)
    cli_channel: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_notify_channel_request(self) -> "NotifyChannelCreateRequest":
        if self.kind == "feishu" and not self.webhook_url:
            raise ValueError("webhook_url_required")
        if self.kind in {"feishu_cli", "clawbot"}:
            if not self.target:
                raise ValueError("notify_target_required")
            if self.kind == "clawbot" and not self.cli_channel:
                raise ValueError("notify_cli_channel_required")
        return self


class NotifyChannelResponse(BaseModel):
    id: str
    name: str
    kind: str
    webhook_url_prefix: str
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


class NotifyChannelListResponse(BaseModel):
    items: list[NotifyChannelResponse]


class NotifyChannelPatch(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool


class PwaDeviceRegisterRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(default="移动设备", min_length=1, max_length=64)
    subscription: dict[str, Any]


class PwaDeviceResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    status: Literal["active", "revoked"]


class PwaDeviceListResponse(BaseModel):
    items: list[PwaDeviceResponse]


class InventoryAuditItemResponse(BaseModel):
    object_id: str
    name: str
    path: str | None
    size_bytes: int | None
    resolution: str | None


class InventoryAuditGroupResponse(BaseModel):
    group_id: str
    kind: Literal["exact_duplicate", "multi_version"]
    items: list[InventoryAuditItemResponse]
    reclaimable_bytes: int
    keep_object_id: str | None


class InventoryAuditReportResponse(BaseModel):
    groups: list[InventoryAuditGroupResponse]
    duplicate_count: int
    multi_version_count: int
    reclaimable_bytes: int


class InventoryDedupeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    object_ids: list[str] = Field(min_length=1, max_length=500)
    confirm: bool


class InventoryDedupeResponse(BaseModel):
    deleted: int = Field(ge=0)
    failed: int = Field(ge=0)
