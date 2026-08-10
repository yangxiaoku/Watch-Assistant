"""Periodic full-tree library scans keep inventory fresh without manual runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import LibraryScanRun, MediaLibrary
from watch_assistant.schemas import LoggingLevel
from watch_assistant.services.library_index import ScanRunState
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
)
from watch_assistant.services.observability import EventLogger, emit_event


@dataclass(frozen=True, slots=True)
class LibraryScanSchedulerResult:
    libraries: int
    scanned: int
    skipped: int
    failed: int


class LibraryScanScheduler:
    """Enqueue one idempotent full-tree scan per library per UTC day.

    The operation idempotency key is date-bucketed, so a completed or running
    scan for the same day is never duplicated. A failed scan is re-queued on
    the next tick, so transient upstream failures self-heal.
    """

    # 当日失败扫描的自动重试冷却期:避免瞬时故障在每次 tick 都热循环重试。
    retry_cooldown = timedelta(minutes=30)

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        operation_service: LibraryScanOperationService,
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._operations = operation_service
        self._event_logger = event_logger

    async def run_due_once(
        self, *, now: datetime | None = None
    ) -> LibraryScanSchedulerResult:
        stamp = now or datetime.now(UTC)
        key_date = stamp.strftime("%Y-%m-%d")
        async with self._session_factory() as session:
            libraries = list(
                (
                    await session.scalars(
                        select(MediaLibrary)
                        .where(
                            MediaLibrary.enabled.is_(True),
                            MediaLibrary.scope_verified.is_(True),
                        )
                        .order_by(MediaLibrary.id)
                    )
                ).all()
            )
        scanned = 0
        skipped = 0
        failed = 0
        for library in libraries:
            try:
                async with self._session_factory() as session:
                    existing = await session.scalar(
                        select(LibraryScanRun).where(
                            LibraryScanRun.library_id == library.id,
                            LibraryScanRun.idempotency_key == f"scheduled-{key_date}",
                        )
                    )
                if existing is not None:
                    if existing.complete or existing.state == ScanRunState.RUNNING.value:
                        # One scan per library per UTC day; a completed scan is
                        # never duplicated, a running scan is not re-queued.
                        skipped += 1
                        continue
                    if existing.state == ScanRunState.CANCELLED.value:
                        # 用户显式取消不自动重试。
                        skipped += 1
                        continue
                    # FAILED:冷却期后重入队自愈(瞬时上游故障,如 115 登录态失效);
                    # enqueue 会把 FAILED 重置回 QUEUED。避免每次 tick 热循环重试。
                    updated = existing.updated_at
                    if updated is not None:
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=UTC)
                        if stamp - updated < self.retry_cooldown:
                            skipped += 1
                            continue
                await self._operations.enqueue(
                    library.id,
                    idempotency_key=f"scheduled-{key_date}",
                )
                scanned += 1
            except Exception:  # noqa: BLE001 - one library must not stop the tick
                failed += 1
                await emit_event(
                    self._event_logger,
                    "library.scan_schedule.failed",
                    level=LoggingLevel.WARNING,
                    fields={"library_id": library.id},
                )
        await emit_event(
            self._event_logger,
            "library.scan_schedule.completed",
            fields={
                "libraries": len(libraries),
                "scanned": scanned,
                "skipped": skipped,
                "failed": failed,
            },
        )
        return LibraryScanSchedulerResult(
            libraries=len(libraries),
            scanned=scanned,
            skipped=skipped,
            failed=failed,
        )

    async def run_forever(
        self, stop_event: asyncio.Event, *, interval_seconds: int
    ) -> None:
        while not stop_event.is_set():
            try:
                await self.run_due_once()
            except Exception:  # noqa: BLE001 - the loop must survive tick errors
                await asyncio.sleep(60)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=float(interval_seconds)
                )
            except TimeoutError:
                continue
