"""只读库存重复检测服务:读取最新完成扫描快照并生成重复报告。

不触发新扫描、不写 115、不写数据库(只 SELECT),仅消费 organization
已完成扫描的 LibraryScanEntry 快照。目标根无已启用且 scope_verified 的
媒体库时 fail-closed 抛 library_scope_unverified。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


class InventoryAuditError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InventoryAuditService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

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


__all__ = ["InventoryAuditError", "InventoryAuditService"]
