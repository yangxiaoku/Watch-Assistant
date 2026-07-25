from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from pwdlib import PasswordHash

from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.api.settings import router
from watch_assistant.security import SESSION_COOKIE, SecurityManager
from watch_assistant.services.p115_credentials import CookieProvider
from watch_assistant.services.p115_settings import (
    P115NeedsAuthError,
    P115SettingsService,
    P115UnavailableError,
)

COOKIE = "UID=uid_A1_456; CID=cid; KID=kid; SEID=seid"


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
    service: P115SettingsService, security: SecurityManager | None = None
):
    app = FastAPI()
    app.state.p115_settings_service = service
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
            "sync_status": "success",
            "last_sync_at": response.json()["cookie"]["last_sync_at"],
        },
        "target_configured": True,
        "max_concurrency": 2,
    }
    assert COOKIE not in response.text
    assert adapter.calls == 0


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
    session_id, csrf_token = security.login("web-secret")
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
    session_id, csrf_token = security.login("web-secret")
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

        def clouddownload_task_list(self, payload):
            self.task_list_calls += 1
            assert payload == {"page": 1}
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
