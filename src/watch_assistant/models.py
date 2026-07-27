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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from watch_assistant.schemas import (
    InspectionBatchStatus,
    InspectionItemStatus,
    MediaType,
    ResourceKind,
    TaskAction,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WebSession(Base):
    __tablename__ = "web_sessions"

    session_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    credential_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TaskState(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


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
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
    """Pending local invalidation event; no consumer is wired in this phase."""

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    def __repr__(self) -> str:
        return (
            "DirectoryDirtyEvent(id=<redacted>, operation_id=<redacted>, "
            "directory_id=<redacted>, "
            f"event_kind={self.event_kind!r}, status={self.status!r})"
        )
