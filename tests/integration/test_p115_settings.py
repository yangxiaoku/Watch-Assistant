import asyncio
import os
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from pwdlib import PasswordHash

from tests.unit.factories import make_security_manager
from watch_assistant.adapters import p115_library_gateway
from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.api.settings_p115 import router
from watch_assistant.security import SESSION_COOKIE, SecurityManager
from watch_assistant.services.p115_credentials import CookieProvider
from watch_assistant.services.p115_settings import (
    P115NeedsAuthError,
    P115SettingsService,
    P115UnavailableError,
)

COOKIE = "UID=uid_A1_456; CID=cid; KID=kid; SEID=seid"
_DEFAULT_CAPABILITIES = {"magnet": True, "share": False}
_UNSET = object()


def _write_cookie(path, value: str = COOKIE) -> None:
    path.write_text(value, encoding="ascii", newline="")
    if os.name != "nt":
        path.chmod(0o600)


class FakeValidationAdapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def validate_read_only(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def _settings_app(
    service: P115SettingsService,
    security: SecurityManager | None = None,
    *,
    p115_ready: object = True,
    push_capabilities: object = _UNSET,
):
    app = FastAPI()
    app.state.p115_settings_service = service
    if p115_ready is not None:
        app.state.p115_ready = p115_ready
    if push_capabilities is _UNSET:
        app.state.push_capabilities = _DEFAULT_CAPABILITIES
    elif push_capabilities is not None:
        app.state.push_capabilities = push_capabilities
    if security is not None:
        app.state.security_manager = security
    else:
        # 鉴权依赖已 fail-closed:未显式传入 manager 的测试夹具默认注入
        # 宽松测试 manager,否则所有请求都会 503 auth_unavailable。
        app.state.security_manager = make_security_manager()
    app.include_router(router)
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://settings.test"
    )


def _security() -> SecurityManager:
    password_hash = PasswordHash.recommended()
    return SecurityManager(
        web_password_hash=password_hash.hash("web-secret"),
        script_token_hash=password_hash.hash("script-secret"),
    )


@pytest.mark.integration
async def test_get_settings_is_local_and_redacts_cookie(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    adapter = FakeValidationAdapter()
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=2,
        adapter=adapter,
    )

    async with await _client(_settings_app(service)) as client:
        response = await client.get("/api/v1/settings/p115")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "ready": True,
        "capabilities": {"magnet": True, "share": False},
        "cookie": {
            "source": "file",
            "configured": True,
            "structure_valid": True,
        },
        "target_configured": True,
        "max_concurrency": 2,
    }
    assert COOKIE not in response.text
    assert adapter.calls == 0


@pytest.mark.integration
async def test_get_settings_requires_auth_and_hides_state_without_session(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
    )
    security = _security()
    app = _settings_app(service, security)

    async with await _client(app) as client:
        anonymous = await client.get("/api/v1/settings/p115")
        session_id, _csrf_token = await security.login_async("web-secret")
        client.cookies.set(SESSION_COOKIE, session_id)
        authenticated = await client.get("/api/v1/settings/p115")

    assert anonymous.status_code == 401
    assert set(anonymous.json()) == {"detail"}
    assert authenticated.status_code == 200


@pytest.mark.integration
async def test_directory_picker_starts_at_the_115_account_root(monkeypatch, tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
    )
    security = _security()
    app = _settings_app(service, security)
    app.state.p115_browsed_directory_ids = set()
    app.state.organization_cookie_provider = object()
    captured: dict[str, object] = {}

    class FakeGateway:
        def __init__(self, _provider, _factory=object, **kwargs):
            captured.update(kwargs)

        async def list_directory(self, directory_id, *, page, page_size):
            assert directory_id == "0"
            assert page == 1
            assert page_size == p115_library_gateway.VERIFIED_BATCH_PAGE_SIZE
            return DirectoryPage(
                items=(
                    LibraryEntry(
                        directory_id="8",
                        file_id=None,
                        parent_id="0",
                        name="媒体库",
                        is_directory=True,
                        size_bytes=0,
                        modified_at=None,
                        pickcode=None,
                    ),
                ),
                page=1,
                page_count=None,
                total=1,
                scan_complete=True,
                state=ScanState.COMPLETE,
                has_more=False,
                next_page=None,
                terminal=True,
            )

    monkeypatch.setattr(
        p115_library_gateway, "P115ReadOnlyDirectoryGateway", FakeGateway
    )
    async with await _client(app) as client:
        session_id, _csrf_token = await security.login_async("web-secret")
        client.cookies.set(SESSION_COOKIE, session_id)
        response = await client.get("/api/v1/settings/p115/directories")

    assert response.status_code == 200
    assert response.json()["root_id"] == "0"
    assert response.json()["parent_id"] == "0"
    assert response.json()["items"] == [{"id": "8", "name": "媒体库"}]
    assert captured["allow_virtual_root"] is True


@pytest.mark.integration
@pytest.mark.parametrize(
    ("p115_ready", "push_capabilities", "expected_ready", "expected_magnet"),
    [
        (False, {"magnet": True, "share": False}, False, False),
        (None, {"magnet": True, "share": False}, False, False),
        (True, None, True, False),
        (True, {"magnet": "true", "share": False}, True, False),
        (True, {"magnet": True}, True, False),
        (True, {"magnet": True, "share": False, "future": True}, True, True),
        (True, {"magnet": True, "share": False}, True, True),
    ],
)
async def test_get_settings_uses_strict_runtime_readiness(
    tmp_path,
    p115_ready: object,
    push_capabilities: object,
    expected_ready: bool,
    expected_magnet: bool,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
    )
    app = _settings_app(
        service,
        p115_ready=p115_ready,
        push_capabilities=push_capabilities,
    )

    async with await _client(app) as client:
        response = await client.get("/api/v1/settings/p115")

    assert response.status_code == 200
    assert response.json()["ready"] is expected_ready
    assert response.json()["capabilities"] == {
        "magnet": expected_magnet,
        "share": False,
    }


@pytest.mark.integration
async def test_validation_timeout_returns_unavailable_without_error_text(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)

    class BlockingAdapter:
        async def validate_read_only(self) -> None:
            await asyncio.Event().wait()

    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=BlockingAdapter(),
        validation_timeout_seconds=0.01,
    )

    result = await service.validate()

    assert result.status == "unavailable"
    assert "timeout" not in repr(result).casefold()


@pytest.mark.integration
async def test_native_validation_timeout_cancels_probe_and_releases_semaphore(
    tmp_path,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)

    class BlockingClient:
        def __init__(self):
            self.active = 0
            self.calls = 0
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        def clouddownload_task_list(self, payload, *, async_):
            assert payload == {"page": 1}
            assert async_ is True
            self.calls += 1

            async def probe():
                self.active += 1
                self.started.set()
                try:
                    if self.calls == 1:
                        await asyncio.Event().wait()
                    return {"state": True, "data": []}
                finally:
                    self.active -= 1
                    if self.calls == 1:
                        self.cancelled.set()

            return probe()

        def close(self):
            return None

    fake = BlockingClient()
    adapter = P115Adapter(CookieProvider(path), 1, client_factory=lambda _cookie: fake)
    assert await adapter.ensure_available()
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
        validation_timeout_seconds=0.01,
    )

    result = await service.validate()
    await asyncio.wait_for(fake.cancelled.wait(), timeout=1)

    assert result.status == "unavailable"
    assert fake.started.is_set()
    assert fake.active == 0

    repeat_service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
        validation_timeout_seconds=1,
    )
    repeated = await repeat_service.validate()

    assert repeated.status == "ready"
    assert fake.calls == 2
    assert fake.active == 0


@pytest.mark.integration
async def test_outer_validation_cancellation_propagates_after_native_probe_cancel(
    tmp_path,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)

    class BlockingClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        def clouddownload_task_list(self, payload, *, async_):
            async def probe():
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()

            return probe()

        def close(self):
            return None

    fake = BlockingClient()
    adapter = P115Adapter(CookieProvider(path), 1, client_factory=lambda _cookie: fake)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
        validation_timeout_seconds=10,
    )
    task = asyncio.create_task(service.validate())
    await asyncio.wait_for(fake.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(fake.cancelled.wait(), timeout=1)


@pytest.mark.integration
async def test_concurrent_validations_keep_one_active_native_probe(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)

    class SerialClient:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.calls = 0
            self.first_started = asyncio.Event()
            self.release = asyncio.Event()

        def clouddownload_task_list(self, payload, *, async_):
            async def probe():
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.calls == 1:
                    self.first_started.set()
                try:
                    await self.release.wait()
                    return {"state": True, "data": []}
                finally:
                    self.active -= 1

            return probe()

        def close(self):
            return None

    fake = SerialClient()
    adapter = P115Adapter(CookieProvider(path), 1, client_factory=lambda _cookie: fake)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
        validation_timeout_seconds=1,
    )
    first = asyncio.create_task(service.validate())
    second = asyncio.create_task(service.validate())
    await asyncio.wait_for(fake.first_started.wait(), timeout=1)
    assert fake.active == 1
    fake.release.set()

    results = await asyncio.gather(first, second)

    assert [result.status for result in results] == ["ready", "ready"]
    assert fake.max_active == 1
    assert fake.active == 0


@pytest.mark.integration
async def test_invalid_cookie_is_failed_and_validation_needs_auth(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path, "UID=uid; CID=cid; KID=kid")
    adapter = FakeValidationAdapter()
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
    )
    security = _security()
    session_id, csrf_token = await security.login_async("web-secret")
    app = _settings_app(service, security)

    async with await _client(app) as client:
        client.cookies.set(SESSION_COOKIE, session_id)
        response = await client.post(
            "/api/v1/settings/p115/validate",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_auth"
    assert response.json()["checked_at"]
    assert adapter.calls == 0


@pytest.mark.integration
async def test_validate_requires_csrf_calls_only_read_only_adapter_and_rate_limits(
    tmp_path,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    adapter = FakeValidationAdapter()
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=adapter,
        validation_limit=1,
    )
    security = _security()
    session_id, csrf_token = await security.login_async("web-secret")
    app = _settings_app(service, security)

    async with await _client(app) as client:
        client.cookies.set(SESSION_COOKIE, session_id)
        missing_csrf = await client.post("/api/v1/settings/p115/validate")
        headers = {"X-CSRF-Token": csrf_token}
        ready = await client.post("/api/v1/settings/p115/validate", headers=headers)
        limited = await client.post("/api/v1/settings/p115/validate", headers=headers)

    assert missing_csrf.status_code == 403
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert limited.status_code == 429
    assert adapter.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (P115NeedsAuthError(), "needs_auth"),
        (P115UnavailableError(), "unavailable"),
        (RuntimeError("opaque"), "unavailable"),
    ],
)
async def test_validate_maps_adapter_failures_without_leaking_errors(
    tmp_path, error: Exception, expected: str
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        adapter=FakeValidationAdapter(error),
    )

    result = await service.validate()

    assert result.status == expected
    assert isinstance(result.checked_at, datetime)
    assert result.checked_at.tzinfo == UTC
    assert "opaque" not in repr(result)


@pytest.mark.integration
async def test_p115_adapter_validation_uses_only_task_list(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)

    class ReadOnlyClient:
        def __init__(self):
            self.task_list_calls = 0
            self.mutating_calls = 0

        async def clouddownload_task_list(self, payload, *, async_):
            self.task_list_calls += 1
            assert payload == {"page": 1}
            assert async_ is True
            return {"state": True, "data": []}

        def clouddownload_task_add_url(self, payload):
            self.mutating_calls += 1
            raise AssertionError("validation must not add tasks")

        def close(self):
            return None

    fake = ReadOnlyClient()
    adapter = P115Adapter(CookieProvider(path), 1, client_factory=lambda _cookie: fake)

    await adapter.validate_read_only()

    assert fake.task_list_calls == 1
    assert fake.mutating_calls == 0


class _FakeCredentialService:
    def __init__(self):
        self.cookie = None
        self.revision = 0

    async def snapshot(self):
        return {"revision": self.revision}

    async def update_p115_cookie(self, cookie, revision):
        self.cookie = cookie
        self.revision = revision


class _FakeQrcodeService:
    def __init__(self, session_id="session-1", cookie=None):
        self.session_id = session_id
        self._cookie = cookie
        self.poll_calls = 0
        self.consumed = False

    async def poll(self, session_id):
        self.poll_calls += 1
        if session_id != self.session_id:
            from watch_assistant.services.p115_qrcode import P115QrcodeError

            raise P115QrcodeError("qrcode_session_not_found")
        if self._cookie is not None:
            return "ready", self._cookie
        return "waiting", None

    async def device_info(self, session_id):
        del session_id
        return "web", "这台电脑"

    async def consume(self, session_id):
        del session_id
        self.consumed = True


class _FakeDeviceService:
    def __init__(self):
        self.devices = []

    async def add_device(self, name, device_code, cookie):
        device = {
            "id": "device-1",
            "name": name,
            "device_code": device_code,
            "active": True,
            "created_at": datetime.now(UTC),
            "last_used_at": datetime.now(UTC),
        }
        self.devices.append(device)
        return device


@pytest.mark.integration
async def test_qrcode_poll_is_read_only_and_save_persists_via_post(tmp_path):
    """M3 加固:GET 轮询不得改写凭据;扫码成功后必须经 POST save 确认入库。"""
    app = FastAPI()
    app.state.security_manager = make_security_manager()
    qrcode = _FakeQrcodeService(cookie=COOKIE)
    credentials = _FakeCredentialService()
    devices = _FakeDeviceService()
    app.state.p115_qrcode_service = qrcode
    app.state.credential_service = credentials
    app.state.p115_login_device_service = devices
    app.include_router(router)

    async with await _client(app) as client:
        # GET 轮询返回 ready,但不得写入任何凭据
        poll = await client.get("/api/v1/settings/p115/qrcode/session-1")
        assert poll.status_code == 200
        body = poll.json()
        assert body["status"] == "ready"
        assert body["device"] is None
        assert credentials.cookie is None, "GET 轮询不得写凭据"
        assert credentials.revision == 0
        assert devices.devices == []
        assert qrcode.consumed is False

        # POST save 才执行凭据持久化
        save = await client.post("/api/v1/settings/p115/qrcode/session-1/save")
        assert save.status_code == 200
        saved = save.json()
        assert saved["status"] == "ready"
        assert saved["device"]["active"] is True
        assert credentials.cookie == COOKIE
        assert len(devices.devices) == 1
        assert devices.devices[0]["active"] is True
        assert qrcode.consumed is True


@pytest.mark.integration
async def test_qrcode_poll_does_not_consume_session(tmp_path):
    """GET 轮询不应消耗 session,否则重复轮询会误报 session_not_found。"""
    app = FastAPI()
    app.state.security_manager = make_security_manager()
    qrcode = _FakeQrcodeService(cookie=COOKIE)
    app.state.p115_qrcode_service = qrcode
    app.state.credential_service = _FakeCredentialService()
    app.state.p115_login_device_service = _FakeDeviceService()
    app.include_router(router)

    async with await _client(app) as client:
        first = await client.get("/api/v1/settings/p115/qrcode/session-1")
        second = await client.get("/api/v1/settings/p115/qrcode/session-1")
        assert first.status_code == 200
        assert second.status_code == 200
