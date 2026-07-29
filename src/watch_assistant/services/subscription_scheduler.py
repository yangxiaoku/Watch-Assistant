"""Safe, search-only subscription checks with durable next-run timestamps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Subscription
from watch_assistant.schemas import SubscriptionStatus
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.subscriptions import (
    SubscriptionConflict,
    SubscriptionNotFound,
    SubscriptionService,
)

_SCHEDULABLE_STATUSES = (
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.MATCHED,
    SubscriptionStatus.NO_MATCH,
)


@dataclass(frozen=True, slots=True)
class SchedulerRunResult:
    due: int
    checked: int
    failed: int


class SubscriptionScheduler:
    """Run due subscription searches without ever creating a push task."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        subscription_service: SubscriptionService,
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._subscriptions = subscription_service
        self._event_logger = event_logger
        self._run_lock = asyncio.Lock()

    async def run_due_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = 25,
    ) -> SchedulerRunResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        current = now or datetime.now(UTC)
        async with self._run_lock:
            async with self._session_factory() as session:
                ids = list(
                    await session.scalars(
                        select(Subscription.id)
                        .where(
                            Subscription.status.in_(_SCHEDULABLE_STATUSES),
                            Subscription.next_check_at.is_not(None),
                            Subscription.next_check_at <= current,
                        )
                        .order_by(Subscription.next_check_at, Subscription.id)
                        .limit(limit)
                    )
                )
            await emit_event(
                self._event_logger,
                "subscription.scheduler_started",
                fields={"total": len(ids), "status": "due"},
            )
            checked = 0
            failed = 0
            for subscription_id in ids:
                try:
                    await self._subscriptions.check(subscription_id)
                except (SubscriptionConflict, SubscriptionNotFound):
                    failed += 1
                except Exception:  # noqa: BLE001 - isolate one due check
                    failed += 1
            checked = len(ids) - failed
            result = SchedulerRunResult(len(ids), checked, failed)
            await emit_event(
                self._event_logger,
                "subscription.scheduler_completed",
                fields={
                    "total": result.due,
                    "count": result.checked,
                    "hidden_count": result.failed,
                    "status": "failed" if result.failed else "completed",
                },
            )
            return result

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float = 60,
    ) -> None:
        if interval_seconds < 5 or interval_seconds > 86_400:
            raise ValueError("interval_seconds must be between 5 and 86400")
        while not stop_event.is_set():
            await self.run_due_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
