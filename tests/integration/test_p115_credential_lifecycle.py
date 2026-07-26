from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.security import SecurityManager

COOKIE = "UID=uid; CID=cid; KID=kid; SEID=seid"


class LifecycleAdapter:
    instances: ClassVar[list[LifecycleAdapter]] = []

    def __init__(self, cookie_provider, target_cid, *, max_concurrency):
        self.cookie_provider = cookie_provider
        self.target_cid = target_cid
        self.max_concurrency = max_concurrency
        self.ready = False
        self.validation_calls = 0
        self.submit_calls = 0
        self.share_calls = 0
        self.delete_calls = 0
        self.closed = False
        type(self).instances.append(self)

    async def validate_cookie(self, _cookie: str) -> None:
        self.validation_calls += 1
        self.ready = True

    async def ensure_available(self) -> bool:
        return self.ready and self.cookie_provider.load() is not None

    async def submit_magnet(self, _url: str):
        self.submit_calls += 1
        raise AssertionError("magnet submission must not run")

    async def save_share(self, _url: str, _password: str | None):
        self.share_calls += 1
        raise AssertionError("share submission must not run")

    async def get_status(self, _remote_ref: str):
        return None

    async def aclose(self) -> None:
        self.closed = True


class ControlledWorker:
    instances: ClassVar[list[ControlledWorker]] = []

    def __init__(self, *_args, **_kwargs):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        type(self).instances.append(self)

    async def recover_expired(self) -> int:
        return 0

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self.stopped.set()


@pytest.mark.integration
async def test_p115_managed_cookie_lifecycle_uses_app_runtime_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    LifecycleAdapter.instances.clear()
    ControlledWorker.instances.clear()
    contract = tmp_path / "tgto.json"
    contract.write_text('{"supported": false}', encoding="utf-8")
    cookie_path = tmp_path / "tgtodrive-cookie"
    cookie_path.write_text("invalid", encoding="ascii", newline="")
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "environment-key",
        "WEB_PASSWORD_HASH": "unused",
        "SCRIPT_TOKEN_HASH": "unused",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TGTO_BASE_URL": "http://tgto.test",
        "TGTO_CONTRACT_PATH": str(contract),
        "CACHE_WARM_ENABLED": "false",
        "P115_ENABLED": "true",
        "P115_COOKIE_PATH": str(cookie_path),
        "P115_TARGET_CID": "1",
        "P115_MAX_CONCURRENCY": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("watch_assistant.app.P115Adapter", LifecycleAdapter)
    monkeypatch.setattr("watch_assistant.app.TaskWorker", ControlledWorker)
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash("password"),
        script_token_hash=password_hash.hash("script"),
    )
    app = create_app(frontend_dir=tmp_path / "missing", security_manager=security)

    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is False
        assert app.state.push_capabilities == {"magnet": False, "share": False}
        assert not hasattr(app.state, "task_worker")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"password": "password"}
            )
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]

            update = await client.put(
                "/api/v1/settings/credentials/p115-cookie",
                json={"value": COOKIE, "revision": 0},
                headers={"X-CSRF-Token": csrf},
            )
            assert update.status_code == 200
            assert COOKIE not in update.text
            assert app.state.p115_ready is True
            assert app.state.push_capabilities == {"magnet": True, "share": False}
            first_worker = app.state.task_worker
            await ControlledWorker.instances[0].started.wait()

            credentials = await client.get("/api/v1/settings/credentials")
            p115_settings = await client.get("/api/v1/settings/p115")
            assert credentials.json()["p115_cookie"]["source"] == "managed"
            assert credentials.json()["p115_cookie"]["ready"] is True
            assert p115_settings.json()["cookie"]["source"] == "managed"
            assert p115_settings.json()["ready"] is True
            assert p115_settings.json()["cookie"]["sync_status"] == "unknown"
            assert p115_settings.json()["cookie"]["last_sync_at"] is None

            update_again = await client.put(
                "/api/v1/settings/credentials/p115-cookie",
                json={"value": COOKIE, "revision": 1},
                headers={"X-CSRF-Token": csrf},
            )
            assert update_again.status_code == 200
            assert COOKIE not in update_again.text
            assert app.state.task_worker is first_worker
            assert len(ControlledWorker.instances) == 1

            reset = await client.post(
                "/api/v1/settings/credentials/p115-cookie/reset",
                json={"revision": 2},
                headers={"X-CSRF-Token": csrf},
            )
            assert reset.status_code == 200
            assert COOKIE not in reset.text
            assert app.state.p115_ready is False
            assert app.state.push_capabilities == {"magnet": False, "share": False}
            assert not hasattr(app.state, "task_worker")
            assert ControlledWorker.instances[0].stopped.is_set()
            p115_settings = await client.get("/api/v1/settings/p115")
            credentials = await client.get("/api/v1/settings/credentials")
            assert p115_settings.json()["cookie"]["source"] == "tgtodrive"
            assert credentials.json()["p115_cookie"]["source"] == "tgtodrive"
            assert p115_settings.json()["ready"] is False
            assert credentials.json()["p115_cookie"]["ready"] is False

            cookie_path.write_text(COOKIE, encoding="ascii", newline="")
            restore = await client.post(
                "/api/v1/settings/credentials/p115-cookie/reset",
                json={"revision": 3},
                headers={"X-CSRF-Token": csrf},
            )
            assert restore.status_code == 200
            assert app.state.p115_ready is True
            assert app.state.push_capabilities == {"magnet": True, "share": False}
            assert len(ControlledWorker.instances) == 2
            await ControlledWorker.instances[1].started.wait()
            p115_settings = await client.get("/api/v1/settings/p115")
            credentials = await client.get("/api/v1/settings/credentials")
            assert p115_settings.json()["cookie"]["source"] == "tgtodrive"
            assert credentials.json()["p115_cookie"]["source"] == "tgtodrive"
            assert p115_settings.json()["ready"] is True
            assert credentials.json()["p115_cookie"]["ready"] is True

        adapter = LifecycleAdapter.instances[0]
        assert adapter.submit_calls == 0
        assert adapter.share_calls == 0
        assert adapter.delete_calls == 0

    assert ControlledWorker.instances[1].stopped.is_set()
    for path in tmp_path.glob("app.db*"):
        if path.is_file():
            assert COOKIE.encode("ascii") not in path.read_bytes()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name.startswith("watch-assistant.log"):
            assert COOKIE.encode("ascii") not in path.read_bytes()
