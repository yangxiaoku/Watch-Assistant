"""Persistence models for the offline, read-only library index."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from watch_assistant.models import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MediaLibrary(Base):
    """A configured, explicitly verified read-only library scope."""

    __tablename__ = "media_libraries"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    root_directory_id: Mapped[str] = mapped_column(String(128))
    scope_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class LibraryScanRun(Base):
    """One idempotent scan and its immutable complete/partial outcome."""

    __tablename__ = "library_scan_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("media_libraries.id"), index=True
    )
    root_directory_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued"
    )
    complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    snapshot_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_read: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_seen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    added_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    changed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class LibraryScanCheckpoint(Base):
    """The last atomically persisted page for a resumable scan."""

    __tablename__ = "library_scan_checkpoints"

    scan_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("library_scan_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    page: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_seen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class LibraryScanEntry(Base):
    """One snapshot observation keyed by stable cloud identity."""

    __tablename__ = "library_scan_entries"

    scan_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("library_scan_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    object_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_directory: Mapped[bool] = mapped_column(Boolean)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LibraryScanDiff(Base):
    """Add/change observations only; this table has no deletion action."""

    __tablename__ = "library_scan_diffs"

    scan_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("library_scan_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    object_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    change_kind: Mapped[str] = mapped_column(String(16))
    path_changed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class OrganizationPlan(Base):
    """Immutable local preview of a possible organization operation."""

    __tablename__ = "organization_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("media_libraries.id"), index=True
    )
    source_scan_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("library_scan_runs.id")
    )
    source_snapshot_revision: Mapped[int] = mapped_column(Integer)
    source_snapshot_json: Mapped[str] = mapped_column(Text)
    target_root: Mapped[str] = mapped_column(Text)
    actions_json: Mapped[str] = mapped_column(Text)
    basis_json: Mapped[str] = mapped_column(Text)
    preconditions_json: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    matcher_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="needs_review", server_default="needs_review"
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
