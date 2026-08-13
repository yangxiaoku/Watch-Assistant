"""Standalone small-file cleanup: preview + recycle (recoverable).

组织自动整理里的未识别小文件删除耦合在 review 计划流程上;当源目录没有
视频文件(``no_video_files``)时整个 run 在 plan 阶段失败,清理从不执行。
这里提供独立入口:预览 = 最近一次完整扫描里低于阈值(``small_file_threshold_mb``)
的非目录文件;应用 = 逐个对父目录实时重验证后走 ``fs_delete`` 回收站
(可恢复,不永久删除)。所有写操作沿用 fail-closed:需 ``confirm``、扫描快照
当前、写契约已开、传输可用。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_c03_live_transport import P115C03LiveTransport
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_delete,
)
from watch_assistant.library_models import LibraryScanEntry, LibraryScanRun
from watch_assistant.services.library_snapshot import verified_latest_scan

logger = logging.getLogger(__name__)

_CLEANUP_CALL_TIMEOUT_SECONDS = 30.0
_MAX_PREVIEW_CANDIDATES = 200


@dataclass(frozen=True)
class SmallFileCandidate:
    file_id: str
    parent_id: str
    name: str
    size_bytes: int | None


class SmallFileCleanupError(ValueError):
    pass


async def _pace(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


async def _resolve_transport(
    value: P115C03LiveTransport | Awaitable[P115C03LiveTransport],
) -> P115C03LiveTransport:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_transport(transport: P115C03LiveTransport) -> None:
    """Release the p115 client owned by an injected cleanup transport."""
    client = getattr(transport, "_client", None)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if callable(close):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - close must never raise upward
            logger.warning("small-file cleanup transport close failed")


class SmallFileCleanupService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        transport_factory: (
            Callable[[], Awaitable[P115C03LiveTransport] | P115C03LiveTransport]
            | None
        ) = None,
    ) -> None:
        self._session_factory = session_factory
        self._transport_factory = transport_factory

    async def preview(
        self,
        library,
        *,
        threshold_bytes: int,
        limit: int = _MAX_PREVIEW_CANDIDATES,
    ) -> tuple[LibraryScanRun, tuple[SmallFileCandidate, ...]]:
        """Return the latest complete snapshot and files below the threshold.

        只读:不写任何数据。快照不是当前完整修订时返回 ``cleanup_snapshot_unavailable``。
        """
        if threshold_bytes <= 0:
            raise SmallFileCleanupError("small_file_threshold_unconfigured")
        if not 1 <= limit <= _MAX_PREVIEW_CANDIDATES:
            raise SmallFileCleanupError("invalid_limit")
        async with self._session_factory() as session:
            scan = await verified_latest_scan(session, library)
            if scan is None:
                raise SmallFileCleanupError("cleanup_snapshot_unavailable")
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry)
                        .where(
                            LibraryScanEntry.scan_run_id == scan.id,
                            LibraryScanEntry.is_directory.is_(False),
                            LibraryScanEntry.size_bytes.is_not(None),
                            LibraryScanEntry.size_bytes < threshold_bytes,
                        )
                        .order_by(
                            LibraryScanEntry.size_bytes.asc(),
                            LibraryScanEntry.object_id.asc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
        candidates = tuple(
            SmallFileCandidate(
                file_id=row.object_id,
                parent_id=row.parent_id or "",
                name=row.name,
                size_bytes=row.size_bytes,
            )
            for row in rows
        )
        return scan, candidates

    async def apply(
        self,
        library,
        *,
        scan_run_id: str,
        file_ids: list[str],
        confirm: bool,
        operation_delay_seconds: float,
    ) -> tuple[int, int]:
        """Recycle confirmed small files, re-verifying each before delete.

        返回 ``(deleted, failed)``。每个文件先在实时父目录列表里复核
        (唯一非目录同名同 id),文件已移动/消失则跳过——fail-closed。
        """
        if not confirm:
            raise SmallFileCleanupError("confirmation_required")
        if self._transport_factory is None:
            raise SmallFileCleanupError("cleanup_unavailable")
        deduped = list(dict.fromkeys(file_ids))
        if not deduped:
            return 0, 0
        async with self._session_factory() as session:
            scan = await session.get(LibraryScanRun, scan_run_id)
            if scan is None or scan.library_id != library.id:
                raise SmallFileCleanupError("snapshot_stale")
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id,
                            LibraryScanEntry.object_id.in_(deduped),
                            LibraryScanEntry.is_directory.is_(False),
                            LibraryScanEntry.size_bytes.is_not(None),
                        )
                    )
                ).all()
            )
        by_id = {row.object_id: row for row in rows}
        candidates = [
            SmallFileCandidate(
                file_id=row.object_id,
                parent_id=row.parent_id or "",
                name=row.name,
                size_bytes=row.size_bytes,
            )
            for file_id in deduped
            if (row := by_id.get(file_id)) is not None
        ]
        if not candidates:
            return 0, 0
        try:
            transport = await _resolve_transport(self._transport_factory())
        except Exception:  # noqa: BLE001 - cleanup fails closed
            raise SmallFileCleanupError("cleanup_unavailable") from None
        deleted = 0
        failed = 0
        try:
            for candidate in candidates:
                await _pace(operation_delay_seconds)
                try:
                    listing = await transport.list_children(
                        candidate.parent_id,
                        timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS,
                    )
                    if not listing.complete:
                        failed += 1
                        continue
                    matches = [
                        entry
                        for entry in listing.entries
                        if entry.file_id == candidate.file_id
                        and not entry.is_directory
                        and entry.name == candidate.name
                    ]
                    if len(matches) != 1:
                        failed += 1
                        continue
                    await _pace(operation_delay_seconds)
                    receipt = await transport.execute(
                        prepare_delete(candidate.file_id),
                        timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS,
                    )
                    if receipt.status is WriteStatus.SUCCESS:
                        deleted += 1
                    else:
                        failed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one failure must not stop the batch
                    failed += 1
        finally:
            await _close_transport(transport)
        if failed:
            logger.warning(
                "small-file cleanup partial: deleted=%d failed=%d", deleted, failed
            )
        return deleted, failed
