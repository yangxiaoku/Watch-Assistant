"""Periodic full-tree library scans keep inventory fresh without manual runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import LibraryScanRun, MediaLibrary
from watch_assistant.schemas import LoggingLevel
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
                    # One scan per library per UTC day, whatever the outcome;
                    # a failed scan is retried on the next UTC day.
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
