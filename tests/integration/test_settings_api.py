from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import LogCategory, LoggingLevel
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "settings-web-password"
SCRIPT_TOKEN = "settings-script-token"


async def _app(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings-api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        cookie_secure=False,
    )
    tmdb = type("Tmdb", (), {"aclose": lambda self: _noop()})()
    pansou = type("PanSou", (), {"aclose": lambda self: _noop()})()
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security,
        frontend_dir=tmp_path / "missing",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return app, client, database, tmdb, pansou


async def _noop():
    return None


@pytest.mark.integration
async def test_settings_overview_logging_patch_and_redacted_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "test-release")
    app, client, database, tmdb, pansou = await _app(tmp_path)

    unauthorized = await client.get("/api/v1/settings/overview")
    assert unauthorized.status_code == 401
    login = await client.post(
        "/api/v1/auth/login",
        json={"password": WEB_PASSWORD},
    )
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    overview = await client.get("/api/v1/settings/overview")
    assert overview.status_code == 200
    assert overview.json()["release"] == "test-release"
    assert set(overview.json()["capabilities"]) == {"inspection", "magnet", "share"}

    current = await client.get("/api/v1/settings/logging")
    assert current.status_code == 200
    revision = current.json()["revision"]
    patched = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "WARNING", "retention_days": 7, "revision": revision},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["level"] == "WARNING"
    assert patched.json()["revision"] == revision + 1

    conflict = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "ERROR", "revision": revision},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "settings_conflict"

    missing_csrf = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "INFO", "revision": patched.json()["revision"]},
    )
    assert missing_csrf.status_code == 403

    await app.state.settings_service.log_store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.SECURITY,
        message="password=secret token=token-value",
        retention_days=7,
        max_file_mb=5,
    )
    first_logs = await client.get("/api/v1/logs", params={"limit": 1})
    assert first_logs.status_code == 200
    assert len(first_logs.json()["items"]) == 1
    assert first_logs.json()["next_cursor"] is not None
    logs = await client.get(
        "/api/v1/logs",
        params={"limit": 2, "cursor": first_logs.json()["next_cursor"]},
    )
    assert logs.status_code == 200
    assert logs.json()["items"]
    assert logs.json()["items"][0]["id"] < first_logs.json()["items"][0]["id"]
    rendered = repr(first_logs.json()) + repr(logs.json())
    assert "secret" not in rendered
    assert "token-value" not in rendered

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_content_policy_defaults_requires_csrf_and_uses_revision(tmp_path):
    _app_obj, client, database, tmdb, pansou = await _app(tmp_path)
    try:
        assert (await client.get("/api/v1/settings/content-policy")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        csrf = login.json()["csrf_token"]
        response = await client.get("/api/v1/settings/content-policy")
        assert response.status_code == 200
        assert response.json() == {
            "hide_adult_media": True,
            "hide_suspicious_resources": True,
            "hide_low_quality_resources": True,
            "blocked_keywords": [],
            "revision": 0,
        }
        missing_csrf = await client.patch(
            "/api/v1/settings/content-policy",
            json={"revision": 0, "blocked_keywords": ["Foo"]},
        )
        assert missing_csrf.status_code == 403
        updated = await client.patch(
            "/api/v1/settings/content-policy",
            json={
                "revision": 0,
                "hide_adult_media": False,
                "blocked_keywords": ["ＦＯＯ－ＢＡＲ"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 1
        assert updated.json()["blocked_keywords"] == ["foo bar"]
        conflict = await client.patch(
            "/api/v1/settings/content-policy",
            json={"revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "settings_conflict"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_logging_patch_rejects_unknown_fields(tmp_path):
    _app_instance, client, database, tmdb, pansou = await _app(tmp_path)
    response = await client.patch(
        "/api/v1/settings/logging",
        json={"revision": 0, "arbitrary": "value"},
        headers={"Authorization": f"Bearer {SCRIPT_TOKEN}"},
    )
    assert response.status_code == 422
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inspection_setting_persists_and_is_authenticated(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)
    assert (await client.get("/api/v1/settings/inspection")).status_code == 401
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    current = await client.get("/api/v1/settings/inspection")
    assert current.json() == {"auto_start_enabled": True, "revision": 0}
    patched = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": False, "revision": 0},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json() == {"auto_start_enabled": False, "revision": 1}
    conflict = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": True, "revision": 0},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "settings_conflict"
    missing_csrf = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": True, "revision": 1},
    )
    assert missing_csrf.status_code == 403
    rebuilt_security = SecurityManager(
        web_password_hash=app.state.security_manager._web_password_hash,
        script_token_hash=PasswordHash.recommended().hash(SCRIPT_TOKEN),
        cookie_secure=False,
    )
    rebuilt = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=rebuilt_security,
        frontend_dir=tmp_path / "missing-rebuilt",
    )
    rebuilt_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rebuilt), base_url="http://app.test"
    )
    persisted = await rebuilt_client.get(
        "/api/v1/settings/inspection", cookies=client.cookies
    )
    assert persisted.status_code == 200
    assert persisted.json()["auto_start_enabled"] is False
    health = await rebuilt_client.get("/api/v1/health")
    assert health.json()["inspection_auto_start_enabled"] is False
    await rebuilt_client.aclose()
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_health_fails_closed_when_inspection_setting_read_fails(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)

    async def unavailable():
        raise RuntimeError("hidden storage detail")

    app.state.settings_service.get_inspection = unavailable
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["inspection_auto_start_enabled"] is False
    assert "hidden storage detail" not in response.text

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()
