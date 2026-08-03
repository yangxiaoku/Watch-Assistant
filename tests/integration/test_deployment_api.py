from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import SecurityManager


@pytest.mark.integration
async def test_deployment_diagnostics_is_authenticated_and_conservative(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'diagnostics.db'}")
    await initialize_database(database.engine)
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=type("Tmdb", (), {"aclose": lambda self: _noop()})(),
        pansou_client=type("PanSou", (), {"aclose": lambda self: _noop()})(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash("diagnostics-password"),
            script_token_hash=password_hash.hash("diagnostics-token"),
            diagnostics_token="deployment-diagnostics-token",
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        unauthorized = await client.get("/api/v1/deployment/diagnostics")
        assert unauthorized.status_code == 401

        dedicated = await client.get(
            "/api/v1/deployment/diagnostics",
            headers={"Authorization": "Bearer deployment-diagnostics-token"},
        )
        assert dedicated.status_code == 200
        dedicated_scope = await client.get(
            "/api/v1/settings/overview",
            headers={"Authorization": "Bearer deployment-diagnostics-token"},
        )
        assert dedicated_scope.status_code == 401

        login = await client.post(
            "/api/v1/auth/login", json={"password": "diagnostics-password"}
        )
        assert login.status_code == 200
        response = await client.get("/api/v1/deployment/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["release"] == "0123456"
    assert payload["pending_migrations"] == []
    assert payload["database_integrity"] == "supported"
    assert payload["write_safe"] is False
    assert {item["key"] for item in payload["components"]} >= {
        "watch_assistant",
        "database",
        "p115client",
        "tmdb",
    }
    assert str(database.engine.url.database) not in response.text
    assert "diagnostics-token" not in response.text
    assert "deployment-diagnostics-token" not in response.text
    assert "diagnostics-password" not in response.text
    await database.engine.dispose()


async def _noop():
    return None
