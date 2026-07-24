"""SQLAlchemy persistence models."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from watch_assistant.schemas import ResourceKind, TaskAction


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


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


class SearchCache(Base):
    __tablename__ = "search_cache"

    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_ids_json: Mapped[str] = mapped_column(Text)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


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
