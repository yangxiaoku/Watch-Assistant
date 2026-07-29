import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Subscription
from watch_assistant.schemas import (
    MediaType,
    SubscriptionCreateRequest,
    SubscriptionStatus,
)
from watch_assistant.services.subscription_scheduler import SubscriptionScheduler
from watch_assistant.services.subscriptions import SubscriptionService


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def search(self, tmdb_id, *, media_type, refresh, season_number):
        self.calls.append(tmdb_id)
        return SimpleNamespace(results=[])


@pytest.mark.asyncio
async def test_scheduler_checks_only_due_schedulable_subscriptions(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    service = SubscriptionService(database.session_factory, search)
    first = await service.create(SubscriptionCreateRequest(tmdb_id=101, media_type=MediaType.MOVIE))
    second = await service.create(SubscriptionCreateRequest(tmdb_id=202, media_type=MediaType.MOVIE))
    paused = await service.create(SubscriptionCreateRequest(tmdb_id=303, media_type=MediaType.MOVIE))
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        for subscription_id in (first.id, second.id, paused.id):
            item = await session.get(Subscription, subscription_id)
            item.next_check_at = now - timedelta(minutes=1)
        item = await session.get(Subscription, paused.id)
        item.status = SubscriptionStatus.PAUSED
        await session.commit()

    scheduler = SubscriptionScheduler(database.session_factory, service)
    result = await scheduler.run_due_once(now=now, limit=10)

    assert result.due == 2
    assert result.checked == 2
    assert result.failed == 0
    assert set(search.calls) == {101, 202}
    async with database.session_factory() as session:
        updated = await session.get(Subscription, first.id)
        assert updated.next_check_at > now.replace(tzinfo=None)
        assert updated.status == SubscriptionStatus.NO_MATCH
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_rejects_unsafe_limits_and_intervals(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
    await initialize_database(database.engine)
    scheduler = SubscriptionScheduler(
        database.session_factory,
        SubscriptionService(database.session_factory, FakeSearch()),
    )
    with pytest.raises(ValueError, match="limit"):
        await scheduler.run_due_once(limit=0)
    with pytest.raises(ValueError, match="interval_seconds"):
        await scheduler.run_forever(asyncio.Event(), interval_seconds=1)
    await database.engine.dispose()
