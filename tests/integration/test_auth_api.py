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
from watch_assistant.models import Resource, WebSession
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


@pytest.mark.integration
async def test_cookie_session_survives_security_manager_rebuild(tmp_path):
    client_a, database, tmdb, pansou = await _make_auth_client(tmp_path)
    login = await client_a.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client_a.cookies.get("watch_session")
    csrf_token = login.json()["csrf_token"]
    assert session_id
    security_a = client_a._transport.app.state.security_manager

    async with database.session_factory() as session:
        stored = await session.scalar(select(WebSession))
        assert stored is not None
        assert session_id not in stored.session_digest

    await client_a.aclose()
    security_b = SecurityManager(
        web_password_hash=security_a._web_password_hash,
        script_token_hash=security_a._script_token_hash,
    )
    app_b = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security_b,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://app.test"
    ) as client_b:
        client_b.cookies.set("watch_session", session_id)
        me = await client_b.get("/api/v1/auth/me")
        post = await client_b.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert me.status_code == 200
    assert me.json()["csrf_token"] == csrf_token
    assert post.status_code == 200
    await _close(client_b, database, tmdb, pansou)


@pytest.mark.integration
async def test_logout_and_password_change_invalidate_persisted_session(tmp_path):
    client_a, database, tmdb, pansou = await _make_auth_client(tmp_path)
    login = await client_a.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client_a.cookies.get("watch_session")
    csrf_token = login.json()["csrf_token"]
    logout = await client_a.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
    )
    assert logout.status_code == 200
    await client_a.aclose()

    password_hash = PasswordHash.recommended()
    rebuilt = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
    )
    app_b = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=rebuilt,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://app.test"
    ) as client_b:
        client_b.cookies.set("watch_session", session_id)
        after_logout = await client_b.get("/api/v1/auth/me")
    assert after_logout.status_code == 401

    await _close(client_b, database, tmdb, pansou)


@pytest.mark.integration
async def test_expired_session_is_rejected_and_deleted(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client.cookies.get("watch_session")
    assert login.status_code == 200
    async with database.session_factory() as session:
        stored = await session.scalar(select(WebSession))
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    async with database.session_factory() as session:
        assert await session.scalar(select(WebSession)) is None
    assert session_id
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_password_hash_change_invalidates_old_session(tmp_path):
    client_a, database, tmdb, pansou = await _make_auth_client(tmp_path)
    await client_a.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client_a.cookies.get("watch_session")
    await client_a.aclose()

    password_hash = PasswordHash.recommended()
    changed = SecurityManager(
        web_password_hash=password_hash.hash("changed-password"),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
    )
    app_b = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=changed,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://app.test"
    ) as client_b:
        client_b.cookies.set("watch_session", session_id)
        response = await client_b.get("/api/v1/auth/me")
    assert response.status_code == 401
    await _close(client_b, database, tmdb, pansou)


@pytest.mark.integration
async def test_login_does_not_set_cookie_when_session_write_fails(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)
    manager = client._transport.app.state.security_manager

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return None

        def add(self, _record):
            return None

        async def commit(self):
            raise OSError("database unavailable")

    class FailingFactory:
        def __call__(self):
            return FailingSession()

    manager.configure_session_store(FailingFactory())
    response = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    await _close(client, database, tmdb, pansou)
