"""Shared fail-closed checks for STRM snapshot-backed operations."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.library_models import LibraryScanRun
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus

_MUTATING_KINDS = (
    StrmOperationKind.FULL,
    StrmOperationKind.INCREMENTAL,
    StrmOperationKind.CLEANUP,
    StrmOperationKind.SMALL_FILE_CLEANUP,
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


async def source_snapshot_is_current(
    session: AsyncSession,
    *,
    library_id: str,
    source_scan_run_id: str,
    source_snapshot_revision: int,
    include_unsettled: bool = True,
) -> bool:
    """Return whether a source run is still the latest complete snapshot.

    This check is used immediately before a mutating transaction commits.  It
    deliberately re-reads the run instead of relying on an ORM identity-map
    value captured at operation start.

    ``include_unsettled=False``(本地缓存 P1,非破坏性判定专用):cancelled/
    failed 等未结算 run 不阻塞——指纹机制下内容是否变化由目录指纹判定,
    中断/失败的扫描(如部署重启取消的 run)不否定已完成快照的有效性。
    破坏性写操作(STRM 清理等)必须保持默认严格语义。
    """

    source_run = await session.scalar(
        select(LibraryScanRun)
        .where(
            LibraryScanRun.id == source_scan_run_id,
            LibraryScanRun.library_id == library_id,
            LibraryScanRun.state == "completed",
            LibraryScanRun.complete.is_(True),
            LibraryScanRun.snapshot_revision == source_snapshot_revision,
        )
        .execution_options(populate_existing=True)
    )
    if source_run is None:
        return False
    latest_revision = await session.scalar(
        select(func.max(LibraryScanRun.snapshot_revision)).where(
            LibraryScanRun.library_id == library_id,
            # M18: 最新 revision 按 root 过滤。库 root 变更后,旧 root 迟到
            # run 若抢更高 revision,会令新 root 快照恒判非最新导致库操作停摆。
            LibraryScanRun.root_directory_id == source_run.root_directory_id,
            LibraryScanRun.complete.is_(True),
            LibraryScanRun.state == "completed",
        )
    )
    if latest_revision != source_snapshot_revision:
        return False
    if not await current_snapshot_is_unique(
        session,
        library_id=library_id,
        snapshot_revision=source_snapshot_revision,
    ):
        return False
    return not (
        include_unsettled and await has_newer_unsettled_scan(session, source_run)
    )


async def current_snapshot_is_unique(
    session: AsyncSession,
    *,
    library_id: str,
    snapshot_revision: int,
) -> bool:
    """Return false when concurrent completion produced an ambiguous revision.

    Revision numbers are globally unique per library (unique index
    uq_library_scan_run_revision), so the ambiguity check stays library-scoped.
    """

    count = await session.scalar(
        select(func.count(LibraryScanRun.id)).where(
            LibraryScanRun.library_id == library_id,
            LibraryScanRun.complete.is_(True),
            LibraryScanRun.state == "completed",
            LibraryScanRun.snapshot_revision == snapshot_revision,
        )
    )
    return count == 1


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
    "current_snapshot_is_unique",
    "has_newer_unsettled_scan",
    "normalize_playback_url_prefix",
    "source_snapshot_is_current",
]
