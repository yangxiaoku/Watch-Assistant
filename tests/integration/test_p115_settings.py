import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from pwdlib import PasswordHash

from watch_assistant.adapters.p115 import P115Adapter
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
            "source": "tgtodrive",
            "configured": True,
            "structure_valid": True,
            "sync_status": "unknown",
            "last_sync_at": None,
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
async def test_cookie_sync_state_requires_explicit_trusted_values(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    synced_at = datetime(2026, 1, 2, tzinfo=UTC)
    service = P115SettingsService(
        enabled=True,
        cookie_provider=CookieProvider(path),
        cookie_path=path,
        target_configured=True,
        max_concurrency=1,
        sync_status="success",
        last_sync_at=synced_at,
    )

    snapshot = service.snapshot(runtime_ready=True, runtime_magnet_capability=True)

    assert snapshot.cookie.sync_status == "success"
    assert snapshot.cookie.last_sync_at == synced_at


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
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        def clouddownload_task_list(self, payload, *, async_):
            assert payload == {"page": 1}
            assert async_ is True

            async def probe():
                self.active += 1
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.active -= 1
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
        validation_timeout_seconds=0.01,
    )

    result = await service.validate()
    await asyncio.wait_for(fake.cancelled.wait(), timeout=1)

    assert result.status == "unavailable"
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
