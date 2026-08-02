import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import RemoteStatus
from watch_assistant.security import SecurityManager
from watch_assistant.services.p115_credentials import CookieProvider
from watch_assistant.services.tasks import TaskService

MAGNET = "magnet:?xt=urn:btih:" + ("a" * 40)


class FakeTaskAdapter:
    def __init__(self, result=RemoteStatus.ACCEPTED, *, readiness=True):
        self.result = result
        self.readiness = readiness
        self.submit_calls = 0
        self.save_share_calls = 0
        self.status_calls = 0
        self.closed = False

    async def ensure_available(self):
        return self.readiness

    async def submit_magnet(self, url: str):
        self.submit_calls += 1
        assert url == MAGNET
        return type(
            "Result",
            (),
            {
                "status": self.result,
                "remote_ref": "remote-test",
                "error_code": None,
                "error_message": None,
            },
        )()

    async def save_share(self, url: str, password: str | None):
        self.save_share_calls += 1
        raise AssertionError("share adapter must not be called")

    async def get_status(self, remote_ref: str):
        self.status_calls += 1
        return RemoteStatus.ACCEPTED

    async def aclose(self):
        self.closed = True


async def _database(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'p115.db'}")
    await initialize_database(database.engine)
    return database


async def _add_resource(database, crypto, *, resource_id: str, kind: str):
    async with database.session_factory() as session:
        session.add(
            Resource(
                id=resource_id,
                kind=kind,
                canonical_key=f"{kind}:{resource_id}",
                encrypted_url=crypto.encrypt(
                    MAGNET if kind == "magnet" else "https://115.com/s/share"
                ),
                encrypted_password=crypto.encrypt("1234") if kind != "magnet" else None,
                name=resource_id,
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.commit()


async def _app_client(tmp_path: Path, adapter=None):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = type("Tmdb", (), {"aclose": lambda self: asyncio.sleep(0)})()
    pansou = type("PanSou", (), {"aclose": lambda self: asyncio.sleep(0)})()
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        task_adapter=adapter,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return app, client, database, crypto, tmdb, pansou


@pytest.mark.integration
async def test_health_capabilities_default_off_and_fake_magnet_on(tmp_path):
    app = create_app(frontend_dir=tmp_path / "missing")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.get("/api/v1/health")
    assert response.json()["push_supported"] is False
    assert response.json()["push_capabilities"] == {"magnet": False, "share": False}

    adapter = FakeTaskAdapter()
    app, client, database, _crypto, tmdb, pansou = await _app_client(
        tmp_path / "enabled", adapter
    )
    async with client:
        response = await client.get("/api/v1/health")
    assert response.json()["push_supported"] is False
    assert response.json()["push_capabilities"] == {"magnet": True, "share": False}
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_p115_enabled_without_target_cid_fails_startup(tmp_path, monkeypatch):
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb",
        "WEB_PASSWORD_HASH": "web",
        "SCRIPT_TOKEN_HASH": "script",
        "PANSOU_BASE_URL": "http://pansou.test",
        "CACHE_WARM_ENABLED": "false",
        "P115_ENABLED": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("P115_TARGET_CID", raising=False)

    app = create_app(frontend_dir=tmp_path / "missing")
    with pytest.raises(RuntimeError, match="P115_ENABLED requires P115_TARGET_CID"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.integration
async def test_p115_disabled_does_not_construct_adapter_or_worker(
    tmp_path, monkeypatch
):
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb",
        "WEB_PASSWORD_HASH": "web",
        "SCRIPT_TOKEN_HASH": "script",
        "PANSOU_BASE_URL": "http://pansou.test",
        "CACHE_WARM_ENABLED": "false",
        "P115_ENABLED": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("P115_TARGET_CID", raising=False)

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("P115Adapter must remain disabled")

    monkeypatch.setattr("watch_assistant.app.P115Adapter", fail_if_constructed)
    app = create_app(frontend_dir=tmp_path / "missing")

    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "task_adapter")
        assert not hasattr(app.state, "task_worker")
        assert app.state.push_capabilities == {"magnet": False, "share": False}


@pytest.mark.integration
async def test_p115_settings_reuses_runtime_adapter_without_remote_access(
    tmp_path, monkeypatch
):
    cookie_path = tmp_path / "p115-cookie"
    cookie_path.write_text(
        "UID=uid_A1_456; CID=cid; KID=kid; SEID=seid",
        encoding="ascii",
    )
    if os.name != "nt":
        cookie_path.chmod(0o600)
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb",
        "WEB_PASSWORD_HASH": "unused",
        "SCRIPT_TOKEN_HASH": "unused",
        "PANSOU_BASE_URL": "http://pansou.test",
        "CACHE_WARM_ENABLED": "false",
        "P115_ENABLED": "true",
        "P115_COOKIE_PATH": str(cookie_path),
        "P115_TARGET_CID": "1",
        "P115_MAX_CONCURRENCY": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    class FakeRuntimeAdapter:
        def __init__(self, cookie_provider, target_cid, *, max_concurrency):
            self.cookie_provider = cookie_provider
            self.target_cid = target_cid
            self.max_concurrency = max_concurrency
            self.validation_calls = 0

        async def ensure_available(self):
            return True

        async def validate_read_only(self):
            self.validation_calls += 1

        async def aclose(self):
            return None

    monkeypatch.setattr("watch_assistant.app.P115Adapter", FakeRuntimeAdapter)
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash("web-secret"),
        script_token_hash=password_hash.hash("script-secret"),
    )
    app = create_app(
        frontend_dir=tmp_path / "missing",
        security_manager=security,
    )

    async with app.router.lifespan_context(app):
        adapter = app.state.task_adapter
        assert app.state.p115_settings_service._adapter is adapter
        assert (
            app.state.p115_settings_service._cookie_provider is adapter.cookie_provider
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"password": "web-secret"}
            )
            csrf_token = login.json()["csrf_token"]
            settings = await client.get("/api/v1/settings/p115")
            assert adapter.validation_calls == 0
            validation = await client.post(
                "/api/v1/settings/p115/validate",
                headers={"X-CSRF-Token": csrf_token},
            )
            health = await client.get("/api/v1/health")

    assert settings.status_code == 200
    assert settings.json()["ready"] is True
    assert settings.json()["capabilities"] == {"magnet": True, "share": False}
    assert validation.status_code == 200
    assert validation.json()["status"] == "ready"
    assert adapter.validation_calls == 1
    assert health.json()["push_supported"] is False
    assert health.json()["push_capabilities"] == {"magnet": True, "share": False}


@pytest.mark.integration
async def test_magnet_task_reaches_submitted_with_fake_adapter(tmp_path):
    adapter = FakeTaskAdapter()
    app, client, database, crypto, tmdb, pansou = await _app_client(tmp_path, adapter)
    await _add_resource(database, crypto, resource_id="magnet_task", kind="magnet")

    async with client:
        created = await client.post(
            "/api/v1/tasks", json={"resource_id": "magnet_task"}
        )
    assert created.status_code == 202
    await app.state.task_worker.run_once()
    stored = await app.state.task_service.get(created.json()["id"])
    assert stored.state == TaskState.SUBMITTED
    assert stored.remote_ref == "remote-test"
    assert adapter.submit_calls == 1
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_missing_cookie_becomes_needs_auth_without_crashing(tmp_path):
    provider = CookieProvider(tmp_path / "missing-cookie")
    adapter = P115Adapter(provider, 1)
    app, client, database, crypto, tmdb, pansou = await _app_client(tmp_path, adapter)
    await _add_resource(database, crypto, resource_id="needs_auth", kind="magnet")

    async with client:
        created = await client.post("/api/v1/tasks", json={"resource_id": "needs_auth"})
    assert created.status_code == 202
    await app.state.task_worker.run_once()
    stored = await app.state.task_service.get(created.json()["id"])
    assert stored.state == TaskState.NEEDS_AUTH
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_share_create_and_retry_are_blocked_without_database_changes(tmp_path):
    adapter = FakeTaskAdapter()
    _app, client, database, crypto, tmdb, pansou = await _app_client(tmp_path, adapter)
    await _add_resource(database, crypto, resource_id="share_task", kind="115_share")
    service = TaskService(database.session_factory)
    historical, _ = await service.create("share_task")

    async with client:
        create_response = await client.post(
            "/api/v1/tasks", json={"resource_id": "share_task", "force": True}
        )
        retry_response = await client.post(f"/api/v1/tasks/{historical.id}/retry")
    assert create_response.status_code == 503
    assert create_response.json()["detail"] == "push_kind_unsupported"
    assert retry_response.status_code == 503
    assert retry_response.json()["detail"] == "push_kind_unsupported"
    async with database.session_factory() as session:
        tasks = list(await session.scalars(select(Task)))
    assert len(tasks) == 1
    assert tasks[0].state == TaskState.QUEUED
    assert adapter.save_share_calls == 0
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_recovers_before_run_and_closes_adapter(tmp_path):
    adapter = FakeTaskAdapter()
    app, client, database, crypto, tmdb, pansou = await _app_client(tmp_path, adapter)
    await _add_resource(database, crypto, resource_id="recover_task", kind="magnet")
    service = TaskService(database.session_factory)
    task, _ = await service.create("recover_task")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.SUBMITTING
        stored.remote_ref = "remote-existing"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert adapter.status_calls >= 1
    assert adapter.closed is True
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_readiness_failure_disables_magnet_and_worker(tmp_path):
    adapter = FakeTaskAdapter(readiness=False)
    app, client, database, crypto, tmdb, pansou = await _app_client(
        tmp_path / "not-ready", adapter
    )
    await _add_resource(database, crypto, resource_id="not_ready", kind="magnet")

    async with app.router.lifespan_context(app):
        health = await client.get("/api/v1/health")
        response = await client.post("/api/v1/tasks", json={"resource_id": "not_ready"})
        assert health.json()["push_capabilities"] == {
            "magnet": False,
            "share": False,
        }
        assert response.status_code == 503
        assert response.json()["detail"] == "push_kind_unsupported"
        assert getattr(app.state, "task_worker", None) is None

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_readiness_success_keeps_magnet_worker_enabled(tmp_path):
    adapter = FakeTaskAdapter(readiness=True)
    app, client, database, _crypto, tmdb, pansou = await _app_client(
        tmp_path / "ready", adapter
    )

    async with app.router.lifespan_context(app):
        health = await client.get("/api/v1/health")
        assert health.json()["push_capabilities"] == {
            "magnet": True,
            "share": False,
        }
        assert getattr(app.state, "task_worker", None) is not None

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()
