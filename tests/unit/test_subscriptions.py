from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Subscription
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


class FailingSearch(FakeSearch):
    async def search(self, tmdb_id, *, media_type, refresh, season_number):
        self.calls.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "refresh": refresh,
                "season_number": season_number,
            }
        )
        raise RuntimeError("upstream unavailable")


class NamedSearch(FakeSearch):
    """Search 返回带 name 的资源,供整季完整性评估使用。"""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names
        self.resource_ids = [f"res_{index}" for index in range(len(names))]

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
            results=[
                SimpleNamespace(resource_id=resource_id, name=name)
                for resource_id, name in zip(self.resource_ids, self.names, strict=True)
            ],
        )


class FakedSeasonMetadata:
    """模拟 SeasonMetadataService.get;可配置返回内容或抛异常。"""

    def __init__(self, *, raise_error: bool = False) -> None:
        self.raise_error = raise_error
        self.calls: list[tuple[int, int]] = []

    async def get(
        self,
        series_tmdb_id: int,
        season_number: int,
        *,
        language: str = "zh-CN",
        fallback_language: str = "en-US",
        refresh: bool = False,
    ) -> object:
        self.calls.append((series_tmdb_id, season_number))
        if self.raise_error:
            from watch_assistant.services.season_metadata import SeasonMetadataError

            raise SeasonMetadataError("season_metadata_unavailable")
        return SimpleNamespace(
            series_tmdb_id=series_tmdb_id,
            season_number=season_number,
            episode_count=10,
            name=f"Season {season_number}",
        )


class EventRecorder:
    def __init__(self):
        self.events = []

    async def log_event(self, event, **kwargs):
        self.events.append((event, kwargs))


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


async def _add_resource_with_metadata(
    database, resource_id: str, canonical_key: str, metadata_json: str
) -> None:
    """与生产资源一致:带 tmdb/季集元数据(自动推送内容级去重依赖)。"""
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
                metadata_json=metadata_json,
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


@pytest.mark.asyncio
async def test_subscription_check_failure_persists_backoff_and_emits_redacted_event(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions-failure.db'}")
    await initialize_database(database.engine)
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory, FailingSearch(), event_logger=events
    )
    created = await service.create(SubscriptionCreateRequest(tmdb_id=456))

    with pytest.raises(SubscriptionConflict, match="subscription_check_failed"):
        await service.check(created.id)

    async with database.session_factory() as session:
        item = await session.get(Subscription, created.id)
        assert item is not None
        assert item.last_error_code == "search_unavailable"
        assert item.next_check_at is not None
        assert item.next_check_at > datetime.now(UTC).replace(tzinfo=None)
    assert any(
        event == "subscription.check_failed"
        and fields["fields"] == {
            "status": "failed",
            "error_code": "search_unavailable",
            "media_type": "movie",
        }
        and fields["resource_type"] == "subscription"
        and fields["resource_id"] == created.id
        for event, fields in events.events
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_subscription_check_creates_linked_workflow(tmp_path):
    """其他跨任务关联:订阅发现新资源时创建带 subscription_id 的 workflow,
    其 DISCOVERY 阶段同步为 succeeded,在任务中心可见。"""
    from watch_assistant.services.workflows import WorkflowService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-wf.db'}")
    await initialize_database(database.engine)
    workflow_service = WorkflowService(database.session_factory)
    service = SubscriptionService(
        database.session_factory,
        FakeSearch(),
        workflow_service=workflow_service,
    )
    created = await service.create(
        SubscriptionCreateRequest(tmdb_id=123, media_type=MediaType.MOVIE)
    )
    checked = await service.check(created.id)
    assert checked.new_resource_ids == ["res_match"]

    workflows = await workflow_service.list()
    linked = [wf for wf in workflows.items if wf.subscription_id == created.id]
    assert len(linked) == 1
    assert linked[0].tmdb_id == 123
    discovery = next(
        stage for stage in linked[0].stages if stage.stage.value == "discovery"
    )
    assert discovery.status.value == "succeeded"
    assert discovery.child_type == "subscription"
    assert discovery.child_id == created.id
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_auto_pauses_when_season_complete_and_emits_event(tmp_path):
    """用例 A:TV+season 订阅,搜索资源覆盖整季、库存为空 →
    check 后状态 PAUSED,并发出 subscription.auto_paused 事件。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-pause.db'}")
    await initialize_database(database.engine)
    search = NamedSearch(["Show S01 1080p COMPLETE"])
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        season_metadata_service=FakedSeasonMetadata(),
        event_logger=events,
    )
    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123, media_type=MediaType.TV, season_number=1
        )
    )
    checked = await service.check(created.id)
    assert checked.matched_count == 1
    assert checked.subscription.status == SubscriptionStatus.PAUSED
    assert any(
        event == "subscription.auto_paused"
        and fields["resource_id"] == created.id
        and fields["resource_type"] == "subscription"
        and fields["fields"].get("media_type") == "tv"
        and fields["fields"].get("status") == "paused"
        and fields["fields"].get("season_number") == 1
        for event, fields in events.events
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_does_not_pause_when_search_not_complete(tmp_path):
    """用例 B:同名资源但只覆盖单集(不齐)→ 状态 MATCHED,不暂停。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-nopause.db'}")
    await initialize_database(database.engine)
    search = NamedSearch(["Show S01E01"])
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        season_metadata_service=FakedSeasonMetadata(),
        event_logger=events,
    )
    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123, media_type=MediaType.TV, season_number=1
        )
    )
    checked = await service.check(created.id)
    assert checked.subscription.status == SubscriptionStatus.MATCHED
    assert not any(
        event == "subscription.auto_paused" for event, _ in events.events
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_skips_auto_pause_when_season_metadata_unavailable(tmp_path):
    """用例 C:season_metadata 抛异常 → 不评估、不暂停(原 MACHED 逻辑)。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-metadata.db'}")
    await initialize_database(database.engine)
    search = NamedSearch(["Show S01 1080p COMPLETE"])
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        season_metadata_service=FakedSeasonMetadata(raise_error=True),
        event_logger=events,
    )
    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123, media_type=MediaType.TV, season_number=1
        )
    )
    checked = await service.check(created.id)
    assert checked.subscription.status == SubscriptionStatus.MATCHED
    assert not any(
        event == "subscription.auto_paused" for event, _ in events.events
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_never_auto_pauses_movie_subscription(tmp_path):
    """用例 D:movie 订阅 → 不评估、不暂停(回归现有行为)。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-movie.db'}")
    await initialize_database(database.engine)
    search = NamedSearch(["Movie 2024 1080p"])
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        season_metadata_service=FakedSeasonMetadata(),
        event_logger=events,
    )
    created = await service.create(
        SubscriptionCreateRequest(tmdb_id=123, media_type=MediaType.MOVIE)
    )
    checked = await service.check(created.id)
    assert checked.subscription.status == SubscriptionStatus.MATCHED
    assert not any(
        event == "subscription.auto_paused" for event, _ in events.events
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_check_auto_pauses_from_local_inventory_complete(tmp_path, monkeypatch):
    """设计 C.4:搜索资源不齐但本地库存已覆盖全部已播出集 → 自动暂停。
    覆盖 _inventory_identities_for_season 的非空分支;并验证 un-matched 的
    图书馆被过滤掉。"""
    from types import SimpleNamespace as _NS

    from watch_assistant.library_models import (
        LibraryMediaIdentity,
        LibraryScanEntry,
        LibraryScanRun,
        MediaLibrary,
    )

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-inv.db'}")
    await initialize_database(database.engine)
    # 伪造一个已验证的快照 run id(verified_latest_scan 被替换,不查真表,
    # 但仍插入 run 行以满足 LibraryScanEntry 的外键)。
    fake_run_id = "run_fake"
    # 匹配库:tmdb 123 / season 1 覆盖 1..10。
    db_match = "lib_match"
    entry_match = "obj_show_s01"
    db_other = "lib_other"
    entry_other = "obj_other_show"
    now = datetime.now(UTC)
    # 第一批:两个库(先于 run/identity/entry,满足外键依赖)。
    async with database.session_factory() as session:
        session.add_all(
            [
                MediaLibrary(
                    id=db_match,
                    name="匹配库",
                    root_directory_id="root_match",
                    scope_verified=True,
                    enabled=True,
                ),
                MediaLibrary(
                    id=db_other,
                    name="其他库",
                    root_directory_id="root_other",
                    scope_verified=True,
                    enabled=True,
                ),
            ]
        )
        await session.commit()
    # 第二批:匹配库的 run 行(满足 LibraryScanEntry 外键)。
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id=fake_run_id,
                library_id=db_match,
                root_directory_id="root_match",
                idempotency_key="fake_complete_run",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.commit()
    # 第三批:identity + 快照 entry。
    async with database.session_factory() as session:
        session.add_all(
            [
                LibraryMediaIdentity(
                    id="ident_match",
                    library_id=db_match,
                    object_id=entry_match,
                    tmdb_id=123,
                    media_type="tv",
                    season=1,
                    episode_start=1,
                    episode_end=10,
                ),
                LibraryMediaIdentity(
                    id="ident_other",
                    library_id=db_other,
                    object_id=entry_other,
                    tmdb_id=999,
                    media_type="tv",
                    season=1,
                    episode_start=1,
                    episode_end=1,
                ),
                LibraryScanEntry(
                    scan_run_id=fake_run_id,
                    object_type="file",
                    object_id=entry_match,
                    name="Show S01E01-E10.mkv",
                    is_directory=False,
                    size_bytes=1024,
                    modified_at=now,
                ),
            ]
        )
        await session.commit()

    # 伪造 verified_latest_scan:对每个库都返回同一 run(fail-closed 的
    # 快照有效性由 monkeypatch 替身承担,测试只覆盖装载聚合与过滤逻辑)。
    async def _fake_verified_latest_scan(session, library):
        return _NS(id=fake_run_id)

    monkeypatch.setattr(
        "watch_assistant.services.subscriptions.verified_latest_scan",
        _fake_verified_latest_scan,
    )

    # 搜索资源不齐,不能靠名字判定完整;进度由库存覆盖。
    search = NamedSearch(["Show S01E01"])
    events = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        season_metadata_service=FakedSeasonMetadata(),
        event_logger=events,
    )
    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123, media_type=MediaType.TV, season_number=1
        )
    )
    checked = await service.check(created.id)
    assert checked.matched_count == 1
    assert checked.subscription.status == SubscriptionStatus.PAUSED
    assert any(
        event == "subscription.auto_paused"
        and fields["fields"].get("season_number") == 1
        for event, fields in events.events
    )
    await database.engine.dispose()


class FakeTaskService:
    def __init__(self) -> None:
        self.created: list[tuple[str, frozenset, str | None, bool]] = []
        self._seen: set[str] = set()

    async def create(
        self,
        resource_id: str,
        *,
        force: bool = False,
        allowed_actions=None,
        workflow_id: str | None = None,
        target_directory_id: str | None = None,
    ):
        reused = resource_id in self._seen
        self._seen.add(resource_id)
        self.created.append(
            (
                resource_id,
                frozenset(allowed_actions or ()),
                target_directory_id,
                reused,
            )
        )
        return SimpleNamespace(id=f"task_{resource_id}"), reused


class FlakyTaskService(FakeTaskService):
    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self.failing = failing

    async def create(self, resource_id, **kwargs):
        if resource_id in self.failing:
            raise RuntimeError("push failed")
        return await super().create(resource_id, **kwargs)


def _push_providers(task_service, capabilities=None):
    async def directory():
        return "dir_push"

    return (
        lambda: task_service,
        lambda: capabilities if capabilities is not None else {"magnet": True},
        directory,
    )


@pytest.mark.asyncio
async def test_auto_mode_creates_push_tasks_for_all_matched_resources(tmp_path):
    """AUTO 模式:本轮匹配的全部资源都创建 115 推送任务(幂等),remind 不创建。

    存量补推语义:第二次检查去重后 new 为空,仍对全部匹配资源尝试创建,
    由 TaskService.create 的幂等保证不重复(REQ-007 SUB-005)。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-auto.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    task_service = FakeTaskService()
    providers = _push_providers(task_service)
    service = SubscriptionService(
        database.session_factory,
        search,
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                season_number=2,
                mode="auto",
            )
        )
        assert created.mode.value == "auto"
        await service.check(created.id)
        assert [item[0] for item in task_service.created] == ["res_match"]
        assert ("dir_push",) == (task_service.created[0][2],)
        # 第二次检查:资源已去重(new 空),但 AUTO 仍对全部匹配尝试创建
        # (幂等,由 TaskService.create 复用已有任务)。
        task_service.created.clear()
        await service.check(created.id)
        assert [item[0] for item in task_service.created] == ["res_match"]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_remind_mode_never_creates_push_tasks(tmp_path):
    """仅提醒模式绝不创建 115 任务(REQ-007 验收)。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-remind.db'}")
    await initialize_database(database.engine)
    task_service = FakeTaskService()
    providers = _push_providers(task_service)
    service = SubscriptionService(
        database.session_factory,
        FakeSearch(),
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                season_number=2,
                mode="remind",
            )
        )
        await service.check(created.id)
        assert task_service.created == []
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_auto_push_skipped_when_capability_unavailable(tmp_path):
    """推送能力不可用(如 115 needs_auth)时整体跳过并记录事件,不创建任务。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-cap.db'}")
    await initialize_database(database.engine)
    task_service = FakeTaskService()
    providers = _push_providers(task_service, capabilities={})
    recorder = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        FakeSearch(),
        event_logger=recorder,
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                mode="auto",
            )
        )
        await service.check(created.id)
        assert task_service.created == []
        assert any(event == "subscription.auto_push_skipped" for event, _ in recorder.events)
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_auto_push_isolates_per_resource_failure(tmp_path):
    """单个资源推送失败不中断其他资源,并记录失败事件。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-flaky.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    search.resource_ids = ["res_a", "res_b", "res_c"]
    task_service = FlakyTaskService(failing={"res_b"})
    providers = _push_providers(task_service)
    recorder = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        event_logger=recorder,
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                mode="auto",
            )
        )
        checked = await service.check(created.id)
        assert checked.subscription.status == SubscriptionStatus.MATCHED
        assert [item[0] for item in task_service.created] == ["res_a", "res_c"]
        assert any(
            event == "subscription.auto_push_failed" for event, _ in recorder.events
        )
        assert any(
            event == "subscription.auto_push_completed" for event, _ in recorder.events
        )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_auto_push_limits_new_tasks_per_check(tmp_path):
    """风控护栏:单轮 AUTO 推送最多创建 N 个新任务,超出记录 limited 事件。

    2026-08 实测整剧订阅一次命中 30+ 资源、31 连发离线下载提交触发 115
    405 风控;每轮限量推送,剩余留给后续轮次。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-limit.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    search.resource_ids = [f"res_{index}" for index in range(8)]
    task_service = FakeTaskService()
    providers = _push_providers(task_service)
    recorder = EventRecorder()
    service = SubscriptionService(
        database.session_factory,
        search,
        event_logger=recorder,
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                mode="auto",
            )
        )
        await service.check(created.id)
        # 8 个资源,单轮上限 5:只创建 5 个新任务
        assert len(task_service.created) == 5
        assert [item[0] for item in task_service.created] == [
            f"res_{index}" for index in range(5)
        ]
        assert any(
            event == "subscription.auto_push_limited" for event, _ in recorder.events
        )
        # 第二轮检查:剩余 3 个资源被补推(前 5 个幂等复用,不占新任务配额)
        task_service.created.clear()
        await service.check(created.id)
        assert [item[0] for item in task_service.created] == [
            f"res_{index}" for index in range(8)
        ]
        assert [item[3] for item in task_service.created] == [True] * 5 + [
            False
        ] * 3
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_auto_push_dedupes_by_content_key(tmp_path):
    """风控护栏:同一 tmdb+季/集 的多版本资源只推第一个版本。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-dedupe.db'}")
    await initialize_database(database.engine)
    metadata = (
        '{"media_type": "tv", "tmdb_id": 60625, "season_number": 1,'
        ' "episode_start": null, "episode_end": null}'
    )
    for resource_id in ("res_s01_x265", "res_s01_x264", "res_s01_web"):
        await _add_resource_with_metadata(
            database, resource_id, f"magnet:{resource_id}", metadata
        )
    search = FakeSearch()
    search.resource_ids = ["res_s01_x265", "res_s01_x264", "res_s01_web"]
    task_service = FakeTaskService()
    providers = _push_providers(task_service)
    service = SubscriptionService(
        database.session_factory,
        search,
        task_service_provider=providers[0],
        push_capabilities_provider=providers[1],
        push_directory_provider=providers[2],
    )
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=60625,
                media_type=MediaType.TV,
                season_number=1,
                mode="auto",
            )
        )
        await service.check(created.id)
        assert [item[0] for item in task_service.created] == ["res_s01_x265"]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_mutate_mode_updates_subscription_mode(tmp_path):
    """mode 动作切换订阅模式;终态订阅不可改。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sub-mode.db'}")
    await initialize_database(database.engine)
    service = SubscriptionService(database.session_factory, FakeSearch())
    try:
        created = await service.create(
            SubscriptionCreateRequest(
                tmdb_id=123,
                media_type=MediaType.TV,
                mode="remind",
            )
        )
        updated = await service.mutate(
            created.id,
            SubscriptionMutationRequest(
                revision=created.revision, mode="auto"
            ),
            "mode",
        )
        assert updated.mode.value == "auto"
        cancelled = await service.mutate(
            updated.id,
            SubscriptionMutationRequest(revision=updated.revision),
            "cancel",
        )
        with pytest.raises(SubscriptionConflict, match="subscription_not_active"):
            await service.mutate(
                cancelled.id,
                SubscriptionMutationRequest(revision=cancelled.revision, mode="auto"),
                "mode",
            )
    finally:
        await database.engine.dispose()
