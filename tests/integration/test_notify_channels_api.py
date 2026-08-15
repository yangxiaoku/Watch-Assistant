"""通知渠道 API 集成测试:装配、鉴权、脱敏回显。"""

from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import AuthContext


async def _make_client(tmp_path: Path, security_manager=None):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'notify_channels.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security_manager or make_security_manager(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, app


@pytest.mark.integration
async def test_notify_channels_wired_and_web_auth_enforced(tmp_path):
    client, _database, app = await _make_client(tmp_path)
    try:
        # 服务与分发器已挂载
        assert app.state.notify_channel_service is not None
        assert app.state.notify_dispatcher is not None
        # require_web_auth 拒绝 bearer 上下文(permissive 测试管理器 via_bearer=True)
        resp = await client.get("/api/v1/notify-channels")
        assert resp.status_code == 403
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_notify_channel_service_encrypts_and_masks_via_state(tmp_path):
    client, database, app = await _make_client(tmp_path)
    try:
        service = app.state.notify_channel_service
        resp = await service.create(
            "我的飞书",
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef1234567890",
        )
        assert "abcdef1234567890" not in resp.webhook_url_prefix
        assert resp.webhook_url_prefix.endswith("7890")
        listed = await service.list()
        assert len(listed) == 1
        assert "open.feishu.cn" in listed[0].webhook_url_prefix
        # 落库必须是密文,不含明文 URL
        async with database.session_factory() as session:
            from watch_assistant.models import NotifyChannel

            row = await session.get(NotifyChannel, listed[0].id)
            assert row is not None
            assert "abcdef1234567890" not in row.webhook_url_encrypted
    finally:
        await client.aclose()


class _WebOnlyTestManager:
    """测试用安全管理器:通过 Web 会话认证,不经过 Bearer。"""

    async def authenticate_async(self, request):
        return AuthContext(identity="internal", via_bearer=False, csrf_token="test-csrf")

    def configure_session_store(self, *args, **kwargs):
        pass

    def configure_event_logger(self, *args, **kwargs):
        pass


class _FakeNotifyDispatcher:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def test_channel(self, channel_id):
        self.calls.append(channel_id)
        return self.result


@pytest.mark.integration
async def test_notify_channel_test_route_returns_ok(tmp_path):
    client, database, app = await _make_client(tmp_path, _WebOnlyTestManager())
    fake = _FakeNotifyDispatcher(result=True)
    app.state.notify_dispatcher = fake
    try:
        resp = await client.post("/api/v1/notify-channels/chan_x/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert fake.calls == ["chan_x"]
    finally:
        await client.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_notify_channel_test_route_returns_false_without_raising(tmp_path):
    client, database, app = await _make_client(tmp_path, _WebOnlyTestManager())
    fake = _FakeNotifyDispatcher(result=False)
    app.state.notify_dispatcher = fake
    try:
        resp = await client.post("/api/v1/notify-channels/chan_x/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}
    finally:
        await client.aclose()
        await database.engine.dispose()
