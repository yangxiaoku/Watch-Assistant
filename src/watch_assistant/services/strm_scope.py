"""Shared fail-closed checks for STRM snapshot-backed operations."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.library_models import LibraryScanRun
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus

_MUTATING_KINDS = (
    StrmOperationKind.FULL,
    StrmOperationKind.INCREMENTAL,
    StrmOperationKind.CLEANUP,
)


async def has_newer_unsettled_scan(
    session: AsyncSession, source_run: LibraryScanRun
) -> bool:
    """Return whether a later scan can still change the source snapshot.

    A completed snapshot is not safe to use for destructive reconciliation when
    a newer scan is queued, running, failed, cancelled, or otherwise incomplete.
    ``created_at`` and ``updated_at`` both matter because an idempotent scan row
    may be re-queued after the original snapshot was completed.
    """

    newer_activity = or_(
        LibraryScanRun.created_at >= source_run.created_at,
        LibraryScanRun.updated_at >= source_run.updated_at,
    )
    row_id = await session.scalar(
        select(LibraryScanRun.id)
        .where(
            LibraryScanRun.library_id == source_run.library_id,
            LibraryScanRun.id != source_run.id,
            newer_activity,
            or_(
                LibraryScanRun.state != "completed",
                LibraryScanRun.complete.is_(False),
            ),
        )
        .limit(1)
    )
    return row_id is not None


async def active_strm_operation_id(
    session: AsyncSession,
    library_id: str,
    *,
    exclude_operation_id: str | None = None,
) -> str | None:
    """Return a mutating STRM operation currently fencing this library."""

    predicates = [
        StrmOperation.library_id == library_id,
        StrmOperation.status == StrmOperationStatus.RUNNING,
        StrmOperation.kind.in_(_MUTATING_KINDS),
    ]
    if exclude_operation_id is not None:
        predicates.append(StrmOperation.id != exclude_operation_id)
    return await session.scalar(select(StrmOperation.id).where(*predicates).limit(1))


def normalize_playback_url_prefix(value: object) -> str:
    """Accept only the stable local playback route, without query secrets."""

    from urllib.parse import urlsplit

    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("invalid_playback_url_prefix")
    if value != value.strip():
        raise ValueError("invalid_playback_url_prefix")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("invalid_playback_url_prefix") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/api/v1/strm/play", "/api/v1/strm/play/"}
    ):
        raise ValueError("invalid_playback_url_prefix")
    return value.rstrip("/") + "/"


__all__ = [
    "active_strm_operation_id",
    "has_newer_unsettled_scan",
    "normalize_playback_url_prefix",
]
