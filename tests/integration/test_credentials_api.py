from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.api.auth import router as auth_router
from watch_assistant.api.credentials import router as credentials_router
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import ApplicationSettings
from watch_assistant.security import SESSION_COOKIE, SecurityManager
from watch_assistant.services.credentials import CredentialService
from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
)

COOKIE = "UID=uid; CID=cid; KID=kid; SEID=seid"


class FakeTmdb:
    def __init__(self) -> None:
        self.key = "environment"
        self.calls: list[str] = []
        self.block: asyncio.Event | None = None

    async def validate_api_key(self, value: str) -> None:
        self.calls.append(value)
        if self.block is not None:
            await self.block.wait()

    def set_api_key(self, value: str) -> None:
        self.key = value


class FakeP115:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ready = True

    async def validate_cookie(self, value: str) -> None:
        self.calls.append(value)

    async def ensure_available(self) -> bool:
        return self.ready


async def _setup(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    fallback = CookieProvider(tmp_path / "tgtodrive-cookie")
    provider = CompositeCookieProvider(fallback)
    tmdb = FakeTmdb()
    p115 = FakeP115()
    service = CredentialService(
        database.session_factory,
        crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=fallback,
        cookie_provider=provider,
        tmdb_client=tmdb,  # type: ignore[arg-type]
        p115_adapter=p115,  # type: ignore[arg-type]
    )
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash("password"),
        script_token_hash=password_hash.hash("script"),
    )
    app = FastAPI()
    app.state.credential_service = service
    app.state.security_manager = security
    app.include_router(credentials_router)
    app.include_router(auth_router)
    return database, crypto, tmdb, p115, security, app


async def _login(client: httpx.AsyncClient, security: SecurityManager) -> str:
    session_id, csrf = security.login("password")
    client.cookies.set(SESSION_COOKIE, session_id)
    return csrf


@pytest.mark.integration
async def test_credentials_are_authenticated_and_never_return_secret(tmp_path: Path):
    database, _crypto, tmdb, _p115, security, app = await _setup(tmp_path)
    secret = "managed-tmdb"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://credentials.test"
    ) as client:
        assert (await client.get("/api/v1/settings/credentials")).status_code == 401
        csrf = await _login(client, security)
        response = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": secret, "revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        body = response.json()
        assert secret not in response.text
        assert body["tmdb"]["source"] == "managed"
        assert tmdb.key == secret
    async with database.session_factory() as session:
        stored = await session.scalar(select(ApplicationSettings))
        assert stored is not None
        assert secret not in (stored.managed_tmdb_key_encrypted or "")
    await database.engine.dispose()


@pytest.mark.integration
async def test_p115_cookie_validation_is_single_call_and_revision_conflict_is_safe(
    tmp_path: Path,
):
    database, _crypto, _tmdb, p115, security, app = await _setup(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://credentials.test"
    ) as client:
        csrf = await _login(client, security)
        response = await client.put(
            "/api/v1/settings/credentials/p115-cookie",
            json={"value": COOKIE, "revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert len(p115.calls) == 1
        conflict = await client.put(
            "/api/v1/settings/credentials/p115-cookie",
            json={"value": COOKIE, "revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert conflict.status_code == 409
    await database.engine.dispose()


@pytest.mark.integration
async def test_cancelled_validation_does_not_change_runtime(tmp_path: Path):
    database, _crypto, tmdb, _p115, _security, _app = await _setup(tmp_path)
    tmdb.block = asyncio.Event()
    service = _app.state.credential_service
    task = asyncio.create_task(service.update_tmdb("candidate", 0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tmdb.key == "environment"
    snapshot = await service.snapshot()
    assert snapshot["tmdb"]["source"] == "environment"  # type: ignore[index]
    await database.engine.dispose()
