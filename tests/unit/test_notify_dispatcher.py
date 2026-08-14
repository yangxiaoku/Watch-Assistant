"""任务级单测:通知分发器(notify_dispatcher)。

覆盖 Task 4 的关键契约:
- dispatch_notify 自己 select 启用渠道并逐行解密 webhook_url,调用
  FeishuChannel.send;返回成功投递的渠道数。
- 单渠道投递失败记 notify.delivery_failed,不抛、不中断其它渠道(fail-open)。
- 无启用渠道时直接返回 0,不抛错。
- notify_resource_found 反查 Subscription 拼标题/正文/link_path 并投递。
- notify_task_available 反查 Task/Workflow 拼标题/正文/链接并投递。
- 任何异常被吞掉记录,绝不中断主流程。
"""

from datetime import UTC, datetime, timedelta

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import (
    MediaType,
    NotifyChannel,
    Resource,
    Subscription,
    SubscriptionStatus,
    Task,
    TaskAction,
    TaskState,
    Workflow,
    WorkflowStatus,
)
from watch_assistant.services.notify_dispatcher import NotifyDispatcher


class FakeCrypto:
    """往返加密;记录 decrypt 调用,便于断言确实解出了明文 URL。"""

    def __init__(self) -> None:
        self.decrypted: list[str] = []

    def encrypt(self, value: str) -> str:
        return f"enc::{value}"

    def decrypt(self, value: str) -> str:
        plain = value[len("enc::"):]
        self.decrypted.append(plain)
        return plain


class FakeFeishuChannel:
    """记录每次 send 的标题/正文/链接,并按 url 决定成功与否。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict[str, object]] = []
        self._fail = fail

    async def send(self, *, title: str, text: str, link_url=None) -> bool:
        self.sent.append({"title": title, "text": text, "link_url": link_url})
        return not self._fail

    async def aclose(self) -> None:
        pass


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def log_event(self, event, **kwargs) -> None:
        self.events.append((event, kwargs))


@pytest.fixture
async def database(tmp_path):
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'notify_dispatcher.db'}")
    await initialize_database(db.engine)
    try:
        yield db
    finally:
        await db.engine.dispose()


async def _add_channel(database, *, enabled=True, fail=False, url="https://open.feishu.cn/hook/chan-1"):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            NotifyChannel(
                id="chan_" + url.rsplit("/", 1)[-1],
                name="渠道",
                kind="feishu",
                webhook_url_encrypted=FakeCrypto().encrypt(url),
                webhook_url_prefix=url[:48] + "…" + url[-4:],
                enabled=enabled,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


async def _add_subscription(database, *, media_type=MediaType.TV, tmdb_id=123):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        item = Subscription(
            id="sub_x",
            tmdb_id=tmdb_id,
            media_type=media_type,
            season_number=2,
            status=SubscriptionStatus.MATCHED,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        await session.commit()


async def _add_resource(database, resource_id="res_x"):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id=resource_id,
                kind="magnet",
                canonical_key="ck",
                encrypted_url="encrypted",
                name="测试资源",
                source="test",
                captured_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()


async def _add_task_and_workflow(database, *, with_workflow_tmdb=True):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        wf = Workflow(
            id="wf_x",
            correlation_id="corr_x",
            media_type=MediaType.TV if with_workflow_tmdb else None,
            tmdb_id=456 if with_workflow_tmdb else None,
            status=WorkflowStatus.IN_PROGRESS,
            created_at=now,
            updated_at=now,
        )
        task = Task(
            id="task_x",
            workflow_id="wf_x",
            resource_id="res_x",
            action=TaskAction.OFFLINE_DOWNLOAD,
            encrypted_url_snapshot="enc",
            state=TaskState.AVAILABLE,
            created_at=now,
            updated_at=now,
        )
        session.add_all([wf, task])
        await session.commit()


def _dispatcher(database, crypto, factory, events=None):
    return NotifyDispatcher(
        database.session_factory,
        crypto,
        event_logger=events,
        channel_factory=factory,
        base_url="http://192.168.6.236:8115",
    )


# ---------- dispatch_notify ----------

@pytest.mark.asyncio
async def test_dispatch_notify_sends_to_all_enabled_and_decrypts(database, crypto, monkeypatch):
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-b")

    def factory(url):
        return FakeFeishuChannel()

    dispatcher = _dispatcher(database, FakeCrypto(), factory)
    try:
        count = await dispatcher.dispatch_notify(title="标题", text="正文", link_path="/tv/123")
    finally:
        await dispatcher.aclose()
    assert count == 2
    assert crypto_decrypted_urls(dispatcher) == ["https://open.feishu.cn/hook/chan-a", "https://open.feishu.cn/hook/chan-b"]


def crypto_decrypted_urls(dispatcher):
    """断言 dispatcher 确实用 crypto.decrypt 解出了明文 webhook_url。"""
    return dispatcher._crypto.decrypted


@pytest.mark.asyncio
async def test_dispatch_notify_fails_one_channel_others_succeed_and_emits_event(database):
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-ok")
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-bad", fail=True)
    events = EventRecorder()
    factories = {}

    def factory(url):
        fail = url.endswith("chan-bad")
        c = FakeFeishuChannel(fail=fail)
        factories[url] = c
        return c

    dispatcher = _dispatcher(database, FakeCrypto(), factory, events=events)
    try:
        count = await dispatcher.dispatch_notify(title="t", text="x", link_path="/tv/1")
    finally:
        await dispatcher.aclose()
    assert count == 1
    assert factories["https://open.feishu.cn/hook/chan-ok"].sent
    assert any(code == "notify.delivery_failed" for code, _ in events.events)


@pytest.mark.asyncio
async def test_dispatch_notify_with_no_channels_returns_zero(database):
    dispatcher = _dispatcher(database, FakeCrypto(), lambda url: FakeFeishuChannel())
    try:
        count = await dispatcher.dispatch_notify(title="t", text="x", link_path="/tv/1")
    finally:
        await dispatcher.aclose()
    assert count == 0


@pytest.mark.asyncio
async def test_dispatch_notify_skips_disabled_channels(database):
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-dis", enabled=False)
    dispatcher = _dispatcher(database, FakeCrypto(), lambda url: FakeFeishuChannel())
    try:
        count = await dispatcher.dispatch_notify(title="t", text="x", link_path="/tv/1")
    finally:
        await dispatcher.aclose()
    assert count == 0


# ---------- notify_resource_found ----------

@pytest.mark.asyncio
async def test_notify_resource_found_tv_builds_fields(database):
    await _add_subscription(database, media_type=MediaType.TV, tmdb_id=777)
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")
    seen = []

    def factory(url):
        c = FakeFeishuChannel()
        c.send = _wrapped_send(seen, c)
        return c

    dispatcher = _dispatcher(database, FakeCrypto(), factory)
    try:
        await dispatcher.notify_resource_found("sub_x")
    finally:
        await dispatcher.aclose()
    assert seen
    call = seen[0]
    assert call["title"] == "追更命中"
    assert call["text"] == "订阅 tv:777 发现新资源"
    assert call["link_url"] == "http://192.168.6.236:8115/tv/777"


def _wrapped_send(seen, channel):
    async def send(*, title, text, link_url=None):
        seen.append({"title": title, "text": text, "link_url": link_url})
        return True
    return send


@pytest.mark.asyncio
async def test_notify_resource_found_movie_uses_movie_path(database):
    await _add_subscription(database, media_type=MediaType.MOVIE, tmdb_id=888)
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")
    seen = []

    def factory(url):
        c = FakeFeishuChannel()
        c.send = _wrapped_send(seen, c)
        return c

    dispatcher = _dispatcher(database, FakeCrypto(), factory)
    try:
        await dispatcher.notify_resource_found("sub_x")
    finally:
        await dispatcher.aclose()
    assert seen[0]["link_url"] == "http://192.168.6.236:8115/movie/888"


@pytest.mark.asyncio
async def test_notify_resource_found_missing_subscription_silent(database):
    dispatcher = _dispatcher(database, FakeCrypto(), lambda url: FakeFeishuChannel())
    try:
        await dispatcher.notify_resource_found("sub_missing")
    finally:
        await dispatcher.aclose()


# ---------- notify_task_available ----------

@pytest.mark.asyncio
async def test_notify_task_available_builds_fields(database):
    await _add_resource(database, "res_x")
    await _add_task_and_workflow(database, with_workflow_tmdb=True)
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")
    seen = []

    def factory(url):
        c = FakeFeishuChannel()
        c.send = _wrapped_send(seen, c)
        return c

    dispatcher = _dispatcher(database, FakeCrypto(), factory)
    try:
        await dispatcher.notify_task_available("task_x")
    finally:
        await dispatcher.aclose()
    assert seen
    call = seen[0]
    assert call["title"] == "资源已入库"
    assert call["text"] == "任务 task_x 已完成入库"
    assert call["link_url"] == "http://192.168.6.236:8115/tv/456"


@pytest.mark.asyncio
async def test_notify_task_available_no_workflow_tmdb_omits_link(database):
    await _add_resource(database, "res_x")
    await _add_task_and_workflow(database, with_workflow_tmdb=False)
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")
    seen = []

    def factory(url):
        c = FakeFeishuChannel()
        c.send = _wrapped_send(seen, c)
        return c

    dispatcher = _dispatcher(database, FakeCrypto(), factory)
    try:
        await dispatcher.notify_task_available("task_x")
    finally:
        await dispatcher.aclose()
    assert seen
    assert seen[0]["link_url"] is None


@pytest.mark.asyncio
async def test_notify_task_available_missing_task_silent(database):
    dispatcher = _dispatcher(database, FakeCrypto(), lambda url: FakeFeishuChannel())
    try:
        await dispatcher.notify_task_available("task_missing")
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatch_failure_never_raises(database):
    await _add_channel(database, url="https://open.feishu.cn/hook/chan-a")

    class Boom:
        async def send(self, **kwargs):
            raise RuntimeError("boom")
        async def aclose(self):
            pass

    dispatcher = _dispatcher(database, FakeCrypto(), lambda url: Boom())
    try:
        count = await dispatcher.dispatch_notify(title="t", text="x", link_path="/tv/1")
    finally:
        await dispatcher.aclose()
    assert count == 0
