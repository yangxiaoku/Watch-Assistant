"""SQLAlchemy persistence models."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from watch_assistant.schemas import (
    InspectionBatchStatus,
    InspectionItemStatus,
    MediaType,
    NotificationSeverity,
    QualityProfileScope,
    ResourceKind,
    SubscriptionMode,
    SubscriptionStatus,
    TaskAction,
    WorkflowStageName,
    WorkflowStageStatus,
    WorkflowStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class ApplicationSettings(Base):
    __tablename__ = "application_settings"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    logging_level: Mapped[str] = mapped_column(String(16), default="INFO")
    retention_days: Mapped[int] = mapped_column(Integer, default=14)
    max_file_mb: Mapped[int] = mapped_column(Integer, default=10)
    inspection_auto_start_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)
    content_policy_json: Mapped[str] = mapped_column(Text, default="{}")
    organization_settings_json: Mapped[str] = mapped_column(Text, default="{}")
    managed_tmdb_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_tmdb_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    managed_p115_cookie_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    managed_p115_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    managed_prowlarr_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    managed_prowlarr_base_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    managed_prowlarr_api_key_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    managed_prowlarr_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class P115LoginDevice(Base):
    """Encrypted, independently switchable 115 login sessions."""

    __tablename__ = "p115_login_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    device_code: Mapped[str] = mapped_column(String(32), nullable=False)
    cookie_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class AuditRecord(Base):
    """Durable record for security-sensitive mutations."""

    __tablename__ = "audit_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_code: Mapped[str] = mapped_column(String(128), index=True)
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    title_zh: Mapped[str] = mapped_column(Text)
    message_zh: Mapped[str] = mapped_column(Text)
    suggestion_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_json: Mapped[str] = mapped_column(Text, default="{}")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_code: Mapped[str] = mapped_column(String(128), index=True)
    severity: Mapped[NotificationSeverity] = mapped_column(
        Enum(NotificationSeverity, values_callable=enum_values, native_enum=False),
        index=True,
    )
    title_zh: Mapped[str] = mapped_column(Text)
    message_zh: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    aggregate_count: Mapped[int] = mapped_column(Integer, default=1)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    muted_event_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    quiet_hours_start: Mapped[str] = mapped_column(String(5), default="23:00", server_default="23:00")
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default="08:00", server_default="08:00")
    quiet_hours_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", server_default="Asia/Shanghai")
    error_bypass_quiet_hours: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class WebhookEndpoint(Base):
    """Encrypted outbound endpoint configuration; never an inbound command hook."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    secret_encrypted: Mapped[str] = mapped_column(Text)
    secret_prefix: Mapped[str] = mapped_column(String(16))
    event_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class WebhookDelivery(Base):
    """Durable per-endpoint delivery state with a stable idempotency key."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("endpoint_id", "event_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    event_code: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PwaDevice(Base):
    """A revocable browser device record; push endpoint data stays encrypted."""

    __tablename__ = "pwa_devices"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_identity: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(64))
    subscription_encrypted: Mapped[str] = mapped_column(Text)
    subscription_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class WebSession(Base):
    __tablename__ = "web_sessions"

    session_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    credential_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AgentToken(Base):
    """Opaque automation credential; the bearer value is never persisted."""

    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    library_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_client_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    call_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TaskState(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[ResourceKind] = mapped_column(
        Enum(ResourceKind, values_callable=enum_values, native_enum=False)
    )
    canonical_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    encrypted_url: Mapped[str] = mapped_column(Text)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    seeders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(100))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    tasks: Mapped[list["Task"]] = relationship(back_populates="resource")
    inspection_items: Mapped[list["InspectionItem"]] = relationship(
        back_populates="resource"
    )


class InspectionBatch(Base):
    __tablename__ = "inspection_batches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True
    )
    status: Mapped[InspectionBatchStatus] = mapped_column(
        Enum(InspectionBatchStatus, values_callable=enum_values, native_enum=False),
        default=InspectionBatchStatus.QUEUED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    items: Mapped[list["InspectionItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class InspectionItem(Base):
    __tablename__ = "inspection_items"

    batch_id: Mapped[str] = mapped_column(
        ForeignKey("inspection_batches.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resources.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[InspectionItemStatus] = mapped_column(
        Enum(InspectionItemStatus, values_callable=enum_values, native_enum=False),
        default=InspectionItemStatus.QUEUED,
        index=True,
    )
    infohash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    video_file_count: Mapped[int] = mapped_column(Integer, default=0)
    video_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    subtitle_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    largest_video_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    batch: Mapped[InspectionBatch] = relationship(back_populates="items")
    resource: Mapped[Resource] = relationship(back_populates="inspection_items")


class MagnetMetadataCache(Base):
    __tablename__ = "magnet_metadata_cache"

    infohash: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[InspectionItemStatus] = mapped_column(
        Enum(InspectionItemStatus, values_callable=enum_values, native_enum=False)
    )
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    video_file_count: Mapped[int] = mapped_column(Integer, default=0)
    video_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    subtitle_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    largest_video_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class SearchCache(Base):
    __tablename__ = "search_cache"

    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_ids_json: Mapped[str] = mapped_column(Text)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    cache_kind: Mapped[str] = mapped_column(
        String(16), default="positive", server_default="positive"
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ResourceSearchJob(Base):
    __tablename__ = "resource_search_jobs"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, values_callable=enum_values, native_enum=False), index=True
    )
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), index=True)
    snapshot_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_plan_version: Mapped[str] = mapped_column(String(16), default="v5")
    cache_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    selected_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SeasonMetadataCache(Base):
    """Cached independent TMDB season detail, separate from resource search."""

    __tablename__ = "season_metadata_cache"

    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    series_tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    season_number: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CacheWarmState(Base):
    __tablename__ = "cache_warm_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MovieWatch(Base):
    __tablename__ = "movie_watches"

    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, values_callable=enum_values, native_enum=False),
        primary_key=True,
    )
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_empty_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    found_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "tmdb_id",
            "media_type",
            "season_number",
            "episode_start",
            "episode_end",
            name="uq_subscription_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, values_callable=enum_values, native_enum=False)
    )
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[SubscriptionMode] = mapped_column(
        Enum(SubscriptionMode, values_callable=enum_values, native_enum=False),
        default=SubscriptionMode.REMIND,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, values_callable=enum_values, native_enum=False),
        default=SubscriptionStatus.ACTIVE,
        index=True,
    )
    quality_profile_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_match_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SubscriptionResourceObservation(Base):
    """Durable first/last-seen state for subscription resource deduplication."""

    __tablename__ = "subscription_resource_observations"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "canonical_key",
            name="uq_subscription_resource_observation_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[str] = mapped_column(String(40), index=True)
    canonical_key: Mapped[str] = mapped_column(String(255), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class QualityProfile(Base):
    __tablename__ = "quality_profiles"
    __table_args__ = (
        UniqueConstraint("scope", "scope_key", "name", name="uq_quality_profile_name"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    scope: Mapped[QualityProfileScope] = mapped_column(
        Enum(QualityProfileScope, values_callable=enum_values, native_enum=False),
        default=QualityProfileScope.GLOBAL,
        index=True,
    )
    scope_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rules_json: Mapped[str] = mapped_column(Text, default="{}")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SourceReliability(Base):
    __tablename__ = "source_reliability"

    source: Mapped[str] = mapped_column(String(100), primary_key=True)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    link_ok_count: Mapped[int] = mapped_column(Integer, default=0)
    link_bad_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True
    )
    target_directory_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    resource_id: Mapped[str | None] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[TaskAction] = mapped_column(
        Enum(TaskAction, values_callable=enum_values, native_enum=False)
    )
    encrypted_url_snapshot: Mapped[str] = mapped_column(Text)
    encrypted_password_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[TaskState] = mapped_column(
        "status",
        Enum(TaskState, values_callable=enum_values, native_enum=False),
        default=TaskState.QUEUED,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    remote_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    resource: Mapped[Resource | None] = relationship(back_populates="tasks")


class Workflow(Base):
    """Top-level local workflow that links independent child operations."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    media_type: Mapped[MediaType | None] = mapped_column(
        Enum(MediaType, values_callable=enum_values, native_enum=False), nullable=True
    )
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(WorkflowStatus, values_callable=enum_values, native_enum=False),
        default=WorkflowStatus.IN_PROGRESS,
        index=True,
    )
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    stages: Mapped[list["WorkflowStage"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowStage.id"
    )


class WorkflowStage(Base):
    """One durable stage in a workflow timeline."""

    __tablename__ = "workflow_stages"
    __table_args__ = (UniqueConstraint("workflow_id", "stage"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[WorkflowStageName] = mapped_column(
        Enum(WorkflowStageName, values_callable=enum_values, native_enum=False)
    )
    # Legacy workflow databases still require this column on inserts.
    stage_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[WorkflowStageStatus] = mapped_column(
        Enum(WorkflowStageStatus, values_callable=enum_values, native_enum=False),
        default=WorkflowStageStatus.PENDING,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    child_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    child_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    workflow: Mapped[Workflow] = relationship(back_populates="stages")


class StrmOperationKind(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    CLEANUP = "cleanup"


class StrmOperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StrmOperation(Base):
    """Durable state and counters for one STRM synchronization request."""

    __tablename__ = "strm_operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # These are application-level links so a failed request can be recorded
    # even when its scan or workflow does not exist.
    library_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[StrmOperationKind] = mapped_column(
        Enum(StrmOperationKind, values_callable=enum_values, native_enum=False),
        nullable=False,
        index=True,
    )
    source_scan_run_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True
    )
    status: Mapped[StrmOperationStatus] = mapped_column(
        Enum(StrmOperationStatus, values_callable=enum_values, native_enum=False),
        default=StrmOperationStatus.QUEUED,
        server_default=StrmOperationStatus.QUEUED.value,
        nullable=False,
        index=True,
    )
    generated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retired: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    def __repr__(self) -> str:
        return (
            "StrmOperation(id=<redacted>, library_id=<redacted>, "
            f"kind={self.kind.value!r}, status={self.status.value!r})"
        )


class OrganizationOperationStatus(StrEnum):
    PLANNED = "planned"
    ORGANIZING = "organizing"
    ORGANIZED = "organized"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class OrganizationOperation(Base):
    """Durable local state for a future organization execution."""

    __tablename__ = "organization_operations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("organization_plans.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    plan_revision: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[OrganizationOperationStatus] = mapped_column(
        Enum(
            OrganizationOperationStatus,
            values_callable=enum_values,
            native_enum=False,
        ),
        default=OrganizationOperationStatus.PLANNED,
        server_default=OrganizationOperationStatus.PLANNED.value,
        index=True,
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        status = self.status.value if self.status is not None else None
        return (
            "OrganizationOperation(id=<redacted>, plan_id=<redacted>, "
            f"status={status!r}, revision={self.revision!r})"
        )


class DirectoryDirtyEvent(Base):
    """Durable directory invalidation event consumed by the STRM worker."""

    __tablename__ = "directory_dirty_events"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "directory_id",
            "event_kind",
            name="uq_directory_dirty_event_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("organization_operations.id", ondelete="RESTRICT"), index=True
    )
    directory_id: Mapped[str] = mapped_column(String(128))
    event_kind: Mapped[str] = mapped_column(
        String(32), default="directory_dirty", server_default="directory_dirty"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    def __repr__(self) -> str:
        return (
            "DirectoryDirtyEvent(id=<redacted>, operation_id=<redacted>, "
            "directory_id=<redacted>, "
            f"event_kind={self.event_kind!r}, status={self.status!r}, "
            f"attempts={self.attempts!r})"
        )


class DirectoryDirtyGeneration(Base):
    """One coalesced dirty lease per managed library directory."""

    __tablename__ = "directory_dirty_generations"
    __table_args__ = (
        UniqueConstraint(
            "library_id",
            "directory_id",
            name="uq_directory_dirty_generation_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True
    )
    directory_id: Mapped[str] = mapped_column(String(128), index=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("organization_operations.id", ondelete="RESTRICT"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    claimed_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return (
            "DirectoryDirtyGeneration(id=<redacted>, library_id=<redacted>, "
            "directory_id=<redacted>, "
            f"generation={self.generation!r}, status={self.status!r})"
        )
