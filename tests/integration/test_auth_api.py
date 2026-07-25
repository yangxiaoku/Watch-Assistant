from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "web-secret"
SCRIPT_TOKEN = "script-secret"


class FakeTaskAdapter:
    async def submit_magnet(self, url: str):
        raise AssertionError("task adapter should not run in auth tests")

    async def save_share(self, url: str, password: str | None):
        raise AssertionError("share adapter must not run in auth tests")

    async def get_status(self, remote_ref: str):
        return None


async def _make_auth_client(tmp_path, *, push_limit=10):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_auth",
                kind="magnet",
                canonical_key="magnet:auth",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:auth"),
                name="Movie",
                source="PanSou",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        cookie_secure=False,
        push_limit=push_limit,
    )
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security,
        task_adapter=FakeTaskAdapter(),
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
async def test_invalid_web_password_returns_401(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)

    response = await client.post("/api/v1/auth/login", json={"password": "wrong"})

    assert response.status_code == 401
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_cookie_session_requires_csrf_for_writes(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})

    missing_csrf = await client.post("/api/v1/tasks", json={"resource_id": "res_auth"})
    accepted = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_auth"},
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )

    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    me = await client.get("/api/v1/auth/me")
    assert me.json()["csrf_token"] == login.json()["csrf_token"]
    assert missing_csrf.status_code == 403
    assert accepted.status_code == 202
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_script_token_is_bearer_only_and_rate_limited(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path, push_limit=1)

    bad_bearer = await client.get(
        "/api/v1/tasks", headers={"Authorization": "Bearer bad"}
    )
    client.cookies.set("watch_session", SCRIPT_TOKEN)
    token_as_cookie = await client.get("/api/v1/tasks")
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {SCRIPT_TOKEN}"}
    first = await client.post(
        "/api/v1/tasks", json={"resource_id": "res_auth"}, headers=headers
    )
    limited = await client.post(
        "/api/v1/tasks", json={"resource_id": "res_auth"}, headers=headers
    )

    assert bad_bearer.status_code == 401
    assert token_as_cookie.status_code == 401
    assert first.status_code == 202
    assert limited.status_code == 429
    await _close(client, database, tmdb, pansou)
