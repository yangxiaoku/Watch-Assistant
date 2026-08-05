import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Request
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, WebSession
from watch_assistant.security import (
    DEFAULT_ADMIN_USERNAME,
    AuthError,
    SecurityManager,
)

WEB_PASSWORD = "web-secret"
SCRIPT_TOKEN = "script-secret"


class FakeTaskAdapter:
    async def submit_magnet(self, url: str):
        raise AssertionError("task adapter should not run in auth tests")

    async def save_share(self, url: str, password: str | None):
        raise AssertionError("share adapter must not run in auth tests")

    async def get_status_for_task(
        self, remote_ref: str, *, target_directory_id: str | None
    ):
        return None


async def _make_auth_client(
    tmp_path,
    *,
    push_limit=10,
    bootstrap_admin_enabled=False,
    bootstrap_admin_password=None,
    configured_username=DEFAULT_ADMIN_USERNAME,
    configured_password=WEB_PASSWORD,
):
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
        web_password_hash=(
            password_hash.hash(configured_password)
            if configured_password is not None
            else None
        ),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        web_username=configured_username,
        bootstrap_admin_enabled=bootstrap_admin_enabled,
        bootstrap_admin_password=bootstrap_admin_password,
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
    resources = await client.get("/api/v1/media/movie/12345/resources")

    assert response.status_code == 401
    assert resources.status_code == 401
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_bootstrap_credentials_are_rejected_when_disabled(tmp_path):
    bootstrap_password = secrets.token_urlsafe(24)
    client, database, tmdb, pansou = await _make_auth_client(
        tmp_path, bootstrap_admin_password=bootstrap_password
    )

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": bootstrap_password,
        },
    )

    assert response.status_code == 401
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_admin_bootstrap_accepts_admin_credentials_only_when_enabled(tmp_path):
    bootstrap_password = secrets.token_urlsafe(24)
    client, database, tmdb, pansou = await _make_auth_client(
        tmp_path,
        bootstrap_admin_enabled=True,
        bootstrap_admin_password=bootstrap_password,
        configured_password=None,
    )

    accepted = await client.post(
        "/api/v1/auth/login",
        json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": bootstrap_password,
        },
    )
    wrong_username = await client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": bootstrap_password},
    )
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"username": DEFAULT_ADMIN_USERNAME, "password": "wrong"},
    )
    repeated = await client.post(
        "/api/v1/auth/login",
        json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": bootstrap_password,
        },
    )

    assert accepted.status_code == 200
    assert wrong_username.status_code == 401
    assert wrong_password.status_code == 401
    assert repeated.status_code == 200
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_admin_bootstrap_accepts_the_explicit_local_default_password(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(
        tmp_path,
        bootstrap_admin_enabled=True,
        bootstrap_admin_password="admin",
        configured_password=None,
    )

    response = await client.post(
        "/api/v1/auth/login",
        json={"username": DEFAULT_ADMIN_USERNAME, "password": "admin"},
    )

    assert response.status_code == 200
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_password_only_login_remains_compatible_with_configured_username(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(
        tmp_path, configured_username="operator"
    )

    password_only = await client.post(
        "/api/v1/auth/login", json={"password": WEB_PASSWORD}
    )
    explicit_username = await client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": WEB_PASSWORD},
    )
    wrong_username = await client.post(
        "/api/v1/auth/login",
        json={"username": DEFAULT_ADMIN_USERNAME, "password": WEB_PASSWORD},
    )

    assert password_only.status_code == 200
    assert explicit_username.status_code == 200
    assert wrong_username.status_code == 401
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
        assert (
            stored.session_digest
            == hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        )
        assert all(
            session_id not in str(value)
            for value in (
                stored.session_digest,
                stored.csrf_token,
                stored.credential_fingerprint,
                stored.created_at,
                stored.expires_at,
            )
        )

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


class _FaultingSession:
    def __init__(
        self, *, record=None, read_error=None, delete_error=None, commit_error=None
    ):
        self.record = record
        self.read_error = read_error
        self.delete_error = delete_error
        self.commit_error = commit_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, _key):
        if self.read_error:
            raise self.read_error
        return self.record

    async def delete(self, _record):
        if self.delete_error:
            raise self.delete_error

    async def commit(self):
        if self.commit_error:
            raise self.commit_error


class _FaultingFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


def _cookie_request(session_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/auth/me",
            "raw_path": b"/api/v1/auth/me",
            "query_string": b"",
            "headers": [(b"cookie", f"watch_session={session_id}".encode("ascii"))],
            "client": ("test", 1),
            "server": ("test", 80),
        }
    )


@pytest.mark.integration
async def test_database_read_delete_and_commit_errors_return_503(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)
    await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client.cookies.get("watch_session")
    manager = client._transport.app.state.security_manager

    manager.configure_session_store(
        _FaultingFactory(_FaultingSession(read_error=OSError("read failed")))
    )
    read_error = await client.get("/api/v1/auth/me")
    assert read_error.status_code == 503
    assert "read failed" not in read_error.text

    expired = WebSession(
        session_digest=hashlib.sha256(session_id.encode()).hexdigest(),
        csrf_token="csrf",
        credential_fingerprint=manager._credential_fingerprint,
        created_at=datetime.now(UTC) - timedelta(hours=1),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    manager.configure_session_store(
        _FaultingFactory(_FaultingSession(record=expired, delete_error=OSError()))
    )
    delete_error = await client.get("/api/v1/auth/me")
    assert delete_error.status_code == 503

    manager.configure_session_store(
        _FaultingFactory(_FaultingSession(record=expired, commit_error=OSError()))
    )
    commit_error = await client.get("/api/v1/auth/me")
    assert commit_error.status_code == 503
    assert "read failed" not in delete_error.text
    assert "read failed" not in commit_error.text
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_database_cancellation_propagates(tmp_path):
    client, database, tmdb, pansou = await _make_auth_client(tmp_path)
    await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    session_id = client.cookies.get("watch_session")
    manager = client._transport.app.state.security_manager

    class CancelledFactory:
        def __call__(self):
            raise asyncio.CancelledError

    manager.configure_session_store(CancelledFactory())
    with pytest.raises(asyncio.CancelledError):
        await manager.authenticate_async(_cookie_request(session_id))
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_custom_session_ttl_matches_cookie_and_database(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'ttl.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    password_hash = PasswordHash.recommended()
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    manager = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        session_ttl=timedelta(hours=3),
    )
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=manager,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
    stored = None
    async with database.session_factory() as session:
        stored = await session.scalar(select(WebSession))
    assert response.status_code == 200
    assert "Max-Age=10800" in response.headers["set-cookie"]
    assert stored is not None
    assert int((stored.expires_at - stored.created_at).total_seconds()) == 10800
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_configured_store_rejects_synchronous_session_side_channel(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    await initialize_database(database.engine)
    password_hash = PasswordHash.recommended()
    manager = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
    )
    manager.configure_session_store(database.session_factory)

    with pytest.raises(AuthError) as login_error:
        manager.login(WEB_PASSWORD)
    with pytest.raises(AuthError) as logout_error:
        manager.logout("session-id")
    assert login_error.value.status_code == 503
    assert logout_error.value.status_code == 503
    async with database.session_factory() as session:
        assert await session.scalar(select(WebSession)) is None
    await database.engine.dispose()
