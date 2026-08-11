from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource
from watch_assistant.schemas import (
    MediaType,
    ResourceKind,
    SubscriptionCreateRequest,
    SubscriptionMutationRequest,
    SubscriptionStatus,
)
from watch_assistant.services.subscriptions import (
    SubscriptionConflict,
    SubscriptionNotFound,
    SubscriptionService,
)


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.resource_ids = ["res_match"]

    async def search(self, tmdb_id, *, media_type, refresh, season_number):
        self.calls.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "refresh": refresh,
                "season_number": season_number,
            }
        )
        return SimpleNamespace(
            results=[SimpleNamespace(resource_id=resource_id) for resource_id in self.resource_ids],
        )


async def _add_resource(database, resource_id: str, canonical_key: str) -> None:
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id=resource_id,
                kind=ResourceKind.MAGNET,
                canonical_key=canonical_key,
                encrypted_url="encrypted",
                name=resource_id,
                source="test",
                captured_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_subscription_lifecycle_and_manual_check(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    service = SubscriptionService(database.session_factory, search)

    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123,
            media_type=MediaType.TV,
            season_number=2,
        )
    )
    assert created.status == SubscriptionStatus.ACTIVE
    checked = await service.check(created.id)
    assert checked.matched_count == 1
    assert checked.resource_ids == ["res_match"]
    assert checked.new_resource_ids == ["res_match"]
    assert checked.subscription.status == SubscriptionStatus.MATCHED
    checked_again = await service.check(created.id)
    assert checked_again.new_resource_ids == []
    observations = await service.list_observations(created.id)
    assert len(observations) == 1
    assert observations[0].resource_id == "res_match"
    assert observations[0].seen_count == 2
    with pytest.raises(ValueError, match="limit"):
        await service.list_observations(created.id, limit=0)
    with pytest.raises(SubscriptionNotFound):
        await service.list_observations("sub_missing")
    assert search.calls == [
        {
            "tmdb_id": 123,
            "media_type": MediaType.TV,
            "refresh": True,
            "season_number": 2,
        },
        {
            "tmdb_id": 123,
            "media_type": MediaType.TV,
            "refresh": True,
            "season_number": 2,
        },
    ]

    paused = await service.mutate(
        created.id,
        SubscriptionMutationRequest(revision=checked_again.subscription.revision),
        "pause",
    )
    assert paused.status == SubscriptionStatus.PAUSED
    with pytest.raises(SubscriptionConflict, match="subscription_not_active"):
        await service.check(created.id)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_subscription_observations_dedupe_by_canonical_key_when_resource_id_changes(
    tmp_path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions-canonical.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    await _add_resource(database, "res_old", "magnet:stable-key")
    search.resource_ids = ["res_old"]
    service = SubscriptionService(database.session_factory, search)
    created = await service.create(SubscriptionCreateRequest(tmdb_id=321))
    first = await service.check(created.id)
    assert first.new_resource_ids == ["res_old"]

    async with database.session_factory() as session:
        old = await session.get(Resource, "res_old")
        await session.delete(old)
        await session.commit()
    await _add_resource(database, "res_new", "magnet:stable-key")
    search.resource_ids = ["res_new"]
    second = await service.check(created.id)
    assert second.resource_ids == ["res_new"]
    assert second.new_resource_ids == []
    observations = await service.list_observations(created.id)
    assert observations[0].resource_id == "res_new"
    assert observations[0].seen_count == 2
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_subscription_scope_is_idempotently_unique(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}")
    await initialize_database(database.engine)
    service = SubscriptionService(database.session_factory, FakeSearch())
    request = SubscriptionCreateRequest(tmdb_id=123)
    await service.create(request)
    with pytest.raises(SubscriptionConflict, match="subscription_exists"):
        await service.create(request)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_does_not_revive_subscription_cancelled_during_writeback(
    tmp_path, monkeypatch
):
    """check() 的写回窗口:重读终态之后、commit 之前若订阅被并发取消,
    不得把 CANCELLED 盖回 MATCHED(复活用户意图)。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    from watch_assistant.models import Subscription

    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'subscriptions-revive.db'}"
    )
    await initialize_database(database.engine)
    search = FakeSearch()
    service = SubscriptionService(database.session_factory, search)
    created = await service.create(SubscriptionCreateRequest(tmdb_id=123))

    original_get = AsyncSession.get
    subscription_gets = 0
    injected = False

    async def getting(self, entity, ident, *args, **kwargs):
        nonlocal subscription_gets, injected
        result = await original_get(self, entity, ident, *args, **kwargs)
        if entity is Subscription and not injected:
            subscription_gets += 1
            if subscription_gets == 2:
                # 第二次读取即 check() 搜索完成后的重读(写回前):用另一
                # session 把订阅置为 CANCELLED,模拟读-改-写窗口内的并发取消。
                injected = True
                async with database.session_factory() as other:
                    other_item = await other.get(Subscription, created.id)
                    other_item.status = SubscriptionStatus.CANCELLED
                    other_item.revision += 1
                    await other.commit()
        return result

    monkeypatch.setattr(AsyncSession, "get", getting)

    with pytest.raises(SubscriptionConflict, match="subscription_not_active"):
        await service.check(created.id)

    # 订阅必须保持 CANCELLED,不得被 check 写回覆盖为 MATCHED。
    async with database.session_factory() as session:
        item = await session.get(Subscription, created.id)
        assert item is not None
        assert item.status == SubscriptionStatus.CANCELLED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_movie_subscription_create_keeps_single_row(tmp_path):
    """M2:SQLite 唯一约束对 NULL 季节字段互不冲突,并发创建同一电影
    订阅可插入重复行;部分唯一索引(迁移 070)保证只成功一个。"""
    import asyncio

    from watch_assistant.services.subscriptions import SubscriptionConflict

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-m2.db'}")
    await initialize_database(database.engine)
    service = SubscriptionService(database.session_factory, FakeSearch())
    request = SubscriptionCreateRequest(tmdb_id=456)

    results = await asyncio.gather(
        *(
            _create_maybe_conflict(service, request)
            for _ in range(4)
        ),
        return_exceptions=True,
    )
    succeeded = sum(1 for item in results if not isinstance(item, Exception))
    conflicts = sum(
        1 for item in results if isinstance(item, SubscriptionConflict)
    )
    assert succeeded == 1
    assert conflicts == 3

    from sqlalchemy import func, select

    from watch_assistant.models import Subscription

    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Subscription))
    assert count == 1
    await database.engine.dispose()


async def _create_maybe_conflict(service, request):
    await service.create(request)


@pytest.mark.asyncio
async def test_cancelled_subscription_can_be_recreated(tmp_path):
    """迁移 071:部分唯一索引必须排除已取消行,否则"取消后重新订阅"
    会因索引连同 CANCELLED 行一起唯一而恒 409(电影订阅回归)。"""
    from watch_assistant.services.subscriptions import SubscriptionStatus

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-071.db'}")
    await initialize_database(database.engine)
    service = SubscriptionService(database.session_factory, FakeSearch())
    request = SubscriptionCreateRequest(tmdb_id=789)
    first = await service.create(request)

    async with database.session_factory() as session:
        from watch_assistant.models import Subscription

        stored = await session.get(Subscription, first.id)
        stored.status = SubscriptionStatus.CANCELLED
        stored.next_check_at = None
        await session.commit()

    # 修复前(070):唯一索引含 CANCELLED 行 → 重订恒 IntegrityError → 409
    recreated = await service.create(request)
    assert recreated.id != first.id
    assert recreated.status is SubscriptionStatus.ACTIVE
    await database.engine.dispose()
