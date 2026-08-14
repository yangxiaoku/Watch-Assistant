"""库存重复检测服务:只读报告 + 去重执行(回收站,可逆)。

run_audit 只读盘点重复;apply_dedupe 对用户确认的重复副本逐个复核后
走 fs_delete 回收站删除,复用 small_file_cleanup 的 fail-closed 删除语义:
需 confirm、快照当前、写契约可用;删除前实时复核父目录,文件已移动/消失
则跳过。永不永久删除(回收站可恢复)。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_c03_live_transport import P115C03LiveTransport
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_delete,
)
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.inventory_audit import (
    InventoryAuditEntry,
    InventoryAuditReport,
    build_audit_report,
)
from watch_assistant.services.library_snapshot import verified_latest_scan

logger = logging.getLogger(__name__)

_DEDUPE_CALL_TIMEOUT_SECONDS = 30.0


async def _pace(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


class InventoryAuditError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InventoryAuditService:
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

    def set_transport_factory(
        self,
        factory: Callable[[], Awaitable[P115C03LiveTransport] | P115C03LiveTransport],
    ) -> None:
        """运行时注入写 transport 工厂(写契约就绪后由 app 装配调用)。"""
        self._transport_factory = factory

    async def run_audit(self, target_root_id: str) -> InventoryAuditReport:
        """盘点目标根的已完成扫描快照,返回重复检测报告(只读)。"""
        async with self._session_factory() as session:
            library = await session.scalar(
                select(MediaLibrary).where(
                    MediaLibrary.root_directory_id == target_root_id,
                    MediaLibrary.enabled.is_(True),
                    MediaLibrary.scope_verified.is_(True),
                )
            )
            if library is None:
                raise InventoryAuditError("library_scope_unverified")

            run = await session.scalar(
                select(LibraryScanRun)
                .where(
                    LibraryScanRun.library_id == library.id,
                    LibraryScanRun.state == "completed",
                    LibraryScanRun.complete.is_(True),
                )
                .order_by(LibraryScanRun.created_at.desc())
                .limit(1)
            )
            if run is None:
                return InventoryAuditReport(
                    groups=(),
                    duplicate_count=0,
                    multi_version_count=0,
                    reclaimable_bytes=0,
                )

            rows = list(
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id,
                        LibraryScanEntry.is_directory.is_(False),
                    )
                )
            )

        entries = [
            InventoryAuditEntry(
                object_id=row.object_id,
                name=row.name,
                path=row.path,
                size_bytes=row.size_bytes,
            )
            for row in rows
        ]
        return build_audit_report(entries)

    async def apply_dedupe(
        self,
        target_root_id: str,
        object_ids: list[str],
        confirm: bool,
        *,
        operation_delay_seconds: float = 1.5,
    ) -> tuple[int, int]:
        """把重复副本删除到 115 回收站(可逆),返回 (deleted, failed)。"""
        if not confirm:
            raise InventoryAuditError("confirmation_required")
        if self._transport_factory is None:
            raise InventoryAuditError("dedupe_unavailable")
        deduped = list(dict.fromkeys(object_ids))
        if not deduped:
            return 0, 0
        async with self._session_factory() as session:
            library = await session.scalar(
                select(MediaLibrary).where(
                    MediaLibrary.root_directory_id == target_root_id,
                    MediaLibrary.enabled.is_(True),
                    MediaLibrary.scope_verified.is_(True),
                )
            )
            scan = await verified_latest_scan(session, library) if library else None
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or scan is None
        ):
            raise InventoryAuditError("snapshot_stale")
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == scan.id,
                        LibraryScanEntry.object_id.in_(deduped),
                        LibraryScanEntry.is_directory.is_(False),
                    )
                )
            )
        by_id = {row.object_id: row for row in rows}
        candidates = [
            (file_id, by_id[file_id].parent_id or "", by_id[file_id].name)
            for file_id in deduped
            if file_id in by_id
        ]
        if not candidates:
            return 0, 0

        transport = self._transport_factory()
        if inspect.isawaitable(transport):
            transport = await transport
        deleted = 0
        failed = 0
        try:
            for file_id, parent_id, name in candidates:
                await _pace(operation_delay_seconds)
                try:
                    listing = await transport.list_children(
                        parent_id,
                        timeout_seconds=_DEDUPE_CALL_TIMEOUT_SECONDS,
                    )
                    if not listing.complete:
                        failed += 1
                        continue
                    matches = [
                        entry
                        for entry in listing.entries
                        if entry.file_id == file_id
                        and not entry.is_directory
                        and entry.name == name
                    ]
                    if len(matches) != 1:
                        failed += 1
                        continue
                    await _pace(operation_delay_seconds)
                    receipt = await transport.execute(
                        prepare_delete(file_id),
                        timeout_seconds=_DEDUPE_CALL_TIMEOUT_SECONDS,
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
            client = getattr(transport, "_client", None)
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001 - close must never raise
                    logger.warning("dedupe transport close failed")
        return deleted, failed


__all__ = ["InventoryAuditError", "InventoryAuditService"]
