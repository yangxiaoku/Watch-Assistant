from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import AgentToken, Resource
from watch_assistant.security import SecurityManager


class _NoopTaskAdapter:
    async def submit_magnet(self, _url: str):
        raise AssertionError("task adapter should not run in token tests")

    async def save_share(self, _url: str, _password: str | None):
        raise AssertionError("task adapter should not run in token tests")

    async def get_status(self, _remote_ref: str):
        return None


async def _client(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_agent",
                kind="magnet",
                canonical_key="magnet:agent",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:agent"),
                name="Agent test resource",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash("web-secret"),
        script_token_hash=password_hash.hash("legacy-script"),
    )
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security,
        task_adapter=_NoopTaskAdapter(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou


async def _close(client, database, tmdb, pansou):
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_agent_token_is_one_time_secret_and_scope_is_enforced(tmp_path):
    client, database, tmdb, pansou = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": "web-secret"})
    csrf = login.json()["csrf_token"]
    created = await client.post(
        "/api/v1/agent/tokens",
        json={"name": "readonly", "scopes": ["system:read"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert created.status_code == 201
    raw_token = created.json()["token"]
    token_id = created.json()["item"]["id"]
    listing = await client.get("/api/v1/agent/tokens")
    assert listing.status_code == 200
    assert raw_token not in listing.text
    async with database.session_factory() as session:
        stored = await session.scalar(select(AgentToken).where(AgentToken.id == token_id))
    assert stored is not None
    assert raw_token not in repr(stored)
    assert stored.token_digest != raw_token

    headers = {"Authorization": f"Bearer {raw_token}"}
    capabilities = await client.get("/api/v1/agent/me", headers=headers)
    denied = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_agent"},
        headers=headers,
    )
    backup_denied = await client.get(
        "/api/v1/backups/configuration", headers=headers
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["scopes"] == ["system:read"]
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "missing_scope"
    assert denied.json()["error"]["missing_scopes"] == ["task:write"]
    assert backup_denied.status_code == 403
    assert backup_denied.json()["error"]["code"] == "missing_scope"
    assert backup_denied.json()["error"]["missing_scopes"] == ["settings:read"]
    write_token = await client.post(
        "/api/v1/agent/tokens",
        json={"name": "settings-writer", "scopes": ["settings:write"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert write_token.status_code == 201
    import_scope_check = await client.post(
        "/api/v1/backups/configuration/import",
        json={},
        headers={"Authorization": f"Bearer {write_token.json()['token']}"},
    )
    assert import_scope_check.status_code == 422
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_agent_token_pause_resume_revoke_and_management_boundary(tmp_path):
    client, database, tmdb, pansou = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": "web-secret"})
    csrf = login.json()["csrf_token"]
    created = await client.post(
        "/api/v1/agent/tokens",
        json={"name": "lifecycle", "scopes": ["system:read"]},
        headers={"X-CSRF-Token": csrf},
    )
    raw_token = created.json()["token"]
    token_id = created.json()["item"]["id"]
    headers = {"Authorization": f"Bearer {raw_token}"}

    pause = await client.post(
        f"/api/v1/agent/tokens/{token_id}/pause",
        headers={"X-CSRF-Token": csrf},
    )
    paused = await client.get("/api/v1/agent/me", headers=headers)
    resume = await client.post(
        f"/api/v1/agent/tokens/{token_id}/resume",
        headers={"X-CSRF-Token": csrf},
    )
    resumed = await client.get("/api/v1/agent/me", headers=headers)
    replacement = await client.post(
        f"/api/v1/agent/tokens/{token_id}/rotate",
        headers={"X-CSRF-Token": csrf},
    )
    replacement_token = replacement.json()["token"]
    replacement_id = replacement.json()["item"]["id"]
    old_after_rotate = await client.get("/api/v1/agent/me", headers=headers)
    replacement_headers = {"Authorization": f"Bearer {replacement_token}"}
    replacement_me = await client.get("/api/v1/agent/me", headers=replacement_headers)
    forbidden_create = await client.post(
        "/api/v1/agent/tokens",
        json={"name": "nested", "scopes": ["system:read"]},
        headers=replacement_headers,
    )
    revoke = await client.post(
        f"/api/v1/agent/tokens/{replacement_id}/revoke",
        headers={"X-CSRF-Token": csrf},
    )
    revoked = await client.get("/api/v1/agent/me", headers=replacement_headers)

    assert pause.status_code == 200
    assert paused.status_code == 401
    assert resume.status_code == 200
    assert resumed.status_code == 200
    assert replacement.status_code == 201
    assert old_after_rotate.status_code == 401
    assert replacement_me.status_code == 200
    assert forbidden_create.status_code == 403
    assert revoke.status_code == 200
    assert revoked.status_code == 401
    await _close(client, database, tmdb, pansou)
