"""Persistence models for the offline, read-only library index."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    removed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    """The bounded diff between two complete snapshots.

    A ``removed`` row is only written after a scan has proved completeness.  A
    partial or failed scan must never manufacture a deletion conclusion.
    """

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


class LibraryObjectLedger(Base):
    """Latest availability state for one stable cloud object identity."""

    __tablename__ = "library_object_ledger"
    __table_args__ = (
        UniqueConstraint(
            "library_id",
            "object_type",
            "object_id",
            name="uq_library_object_ledger_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True
    )
    object_type: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(128), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_directory: Mapped[bool] = mapped_column(Boolean)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, index=True
    )
    missing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scan_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("library_scan_runs.id", ondelete="SET NULL"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class LibraryInventoryEvent(Base):
    """Immutable, deduplicated evidence for inventory availability changes."""

    __tablename__ = "library_inventory_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_library_inventory_event_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True
    )
    scan_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("library_scan_runs.id", ondelete="SET NULL"), nullable=True
    )
    object_type: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(128), index=True)
    event_kind: Mapped[str] = mapped_column(String(16), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, index=True
    )


class LibraryMediaIdentity(Base):
    """A local, manually confirmed media identity for one stable file."""

    __tablename__ = "library_media_identities"
    __table_args__ = (
        UniqueConstraint("library_id", "object_id", name="uq_library_media_identity_object"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True
    )
    object_id: Mapped[str] = mapped_column(String(128), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[str] = mapped_column(String(8))
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str] = mapped_column(
        String(16), default="trusted", server_default="trusted"
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class StrmManifestStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    RETIRED = "retired"


class StrmManifestEntry(Base):
    """A local STRM file backed by one current, indexed 115 file."""

    __tablename__ = "strm_manifest_entries"
    __table_args__ = (
        Index(
            "uq_strm_manifest_current_file",
            "library_id",
            "cloud_file_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )

    manifest_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True
    )
    cloud_file_id: Mapped[str] = mapped_column(String(128), index=True)
    cloud_directory_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    pickcode: Mapped[str | None] = mapped_column(Text, nullable=True)
    cloud_relative_path: Mapped[str] = mapped_column(Text)
    local_relative_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default=StrmManifestStatus.PENDING, server_default="pending", index=True
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", index=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
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
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
