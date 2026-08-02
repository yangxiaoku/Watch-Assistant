from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.adapters.tmdb import TmdbAuthError
from watch_assistant.api.auth import router as auth_router
from watch_assistant.api.credentials import router as credentials_router
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import ApplicationSettings
from watch_assistant.schemas import InspectionSettingsPatch
from watch_assistant.security import SESSION_COOKIE, SecurityManager
from watch_assistant.services.credentials import (
    CredentialConflict,
    CredentialRejected,
    CredentialService,
    CredentialValidationUnavailable,
)
from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
)
from watch_assistant.services.settings import SettingsConflict, SettingsService

COOKIE = "UID=uid; CID=cid; KID=kid; SEID=seid"


class FakeTmdb:
    def __init__(self) -> None:
        self.key = "environment"
        self.calls: list[str] = []
        self.block: asyncio.Event | None = None
        self.error: BaseException | None = None

    async def validate_api_key(self, value: str) -> None:
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        if self.block is not None:
            await self.block.wait()

    def set_api_key(self, value: str) -> None:
        self.key = value


class FakeP115:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ready = True
        self.error: BaseException | None = None
        self.block: asyncio.Event | None = None

    async def validate_cookie(self, value: str) -> None:
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        if self.block is not None:
            await self.block.wait()

    async def ensure_available(self) -> bool:
        return self.ready


async def _setup(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    fallback = CookieProvider(tmp_path / "p115-cookie")
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
        reset = await client.post(
            "/api/v1/settings/credentials/tmdb/reset",
            json={"revision": 1},
            headers={"X-CSRF-Token": csrf},
        )
        assert reset.status_code == 200
        assert reset.json()["tmdb"]["source"] == "environment"
        assert tmdb.key == "environment"
        assert secret not in repr(app.state.credential_service)
    async with database.session_factory() as session:
        stored = await session.scalar(select(ApplicationSettings))
        assert stored is not None
        assert secret not in (stored.managed_tmdb_key_encrypted or "")
    await database.engine.dispose()
    assert secret.encode("utf-8") not in (tmp_path / "credentials.db").read_bytes()


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
async def test_rebuilt_service_restores_managed_p115_source(tmp_path: Path):
    database, crypto, _tmdb, p115, _security, app = await _setup(tmp_path)
    service = app.state.credential_service
    await service.update_p115_cookie(COOKIE, 0)
    await database.engine.dispose()

    rebuilt_database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}"
    )
    await initialize_database(rebuilt_database.engine)
    fallback = CookieProvider(tmp_path / "missing-fallback")
    rebuilt_provider = CompositeCookieProvider(fallback)
    rebuilt = CredentialService(
        rebuilt_database.session_factory,
        crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=fallback,
        cookie_provider=rebuilt_provider,
        tmdb_client=FakeTmdb(),  # type: ignore[arg-type]
        p115_adapter=FakeP115(),  # type: ignore[arg-type]
    )
    await rebuilt.load_managed()
    snapshot = await rebuilt.snapshot()
    assert snapshot["p115_cookie"]["source"] == "managed"  # type: ignore[index]
    assert COOKIE not in repr(rebuilt)
    assert len(p115.calls) == 1
    await rebuilt_database.engine.dispose()


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


@pytest.mark.integration
async def test_invalid_payload_is_stable_and_csrf_is_enforced(tmp_path: Path):
    database, _crypto, _tmdb, _p115, security, app = await _setup(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://credentials.test"
    ) as client:
        session_id, _csrf = security.login("password")
        client.cookies.set(SESSION_COOKIE, session_id)
        sentinel = "sentinel-secret-value"
        response = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": sentinel, "revision": 0, "extra": sentinel},
        )
        assert response.status_code == 403
        csrf = security._sessions[session_id].csrf_token
        response = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": sentinel, "revision": 0, "extra": sentinel},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422
        assert sentinel not in response.text
        malformed = await client.put(
            "/api/v1/settings/credentials/tmdb",
            content='{"value":"sentinel-secret-value"',
            headers={
                "X-CSRF-Token": csrf,
                "Content-Type": "application/json",
            },
        )
        assert malformed.status_code == 422
        assert sentinel not in malformed.text
        wrong_type = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": {"sentinel": sentinel}, "revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert wrong_type.status_code == 422
        assert sentinel not in wrong_type.text
    await database.engine.dispose()


@pytest.mark.integration
async def test_rejected_unavailable_timeout_and_unsupported_p115_are_stable(
    tmp_path: Path,
):
    database, _crypto, tmdb, p115, _security, app = await _setup(tmp_path)
    service = app.state.credential_service
    tmdb.error = RuntimeError("opaque")
    with pytest.raises(CredentialValidationUnavailable):
        await service.update_tmdb("candidate", 0)
    tmdb.error = TmdbAuthError("rejected")
    tmdb.block = None
    with pytest.raises(CredentialRejected):
        await service.update_tmdb("candidate", 0)
    p115.error = RuntimeError("opaque")
    with pytest.raises(CredentialValidationUnavailable):
        await service.update_p115_cookie(COOKIE, 0)
    service._timeout_seconds = 0.01
    tmdb.error = None
    tmdb.block = asyncio.Event()
    with pytest.raises(CredentialValidationUnavailable):
        await service.update_tmdb("candidate", 0)
    unsupported = CredentialService(
        database.session_factory,
        _crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=service._fallback_cookie_provider,
        tmdb_client=tmdb,  # type: ignore[arg-type]
        p115_adapter=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(CredentialValidationUnavailable):
        await unsupported.update_p115_cookie(COOKIE, 0)
    assert (await service.snapshot())["revision"] == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_two_credentials_same_revision_only_one_commits(tmp_path: Path):
    database, _crypto, tmdb, _p115, _security, app = await _setup(tmp_path)
    service = app.state.credential_service
    first, second = await asyncio.gather(
        service.update_tmdb("first", 0),
        service.update_tmdb("second", 0),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in (first, second)) == 1
    assert (
        sum(isinstance(result, CredentialConflict) for result in (first, second)) == 1
    )
    assert tmdb.key in {"first", "second"}
    await database.engine.dispose()


@pytest.mark.integration
async def test_settings_and_credential_share_revision_lock(tmp_path: Path):
    database, _crypto, _tmdb, _p115, _security, app = await _setup(tmp_path)
    settings = SettingsService(database.session_factory, state_directory=tmp_path)
    credential = app.state.credential_service
    results = await asyncio.gather(
        credential.update_tmdb("credential", 0),
        settings.update_inspection(
            InspectionSettingsPatch(auto_start_enabled=False, revision=0)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert (
        sum(
            isinstance(result, (CredentialConflict, SettingsConflict))
            for result in results
        )
        == 1
    )
    await database.engine.dispose()


@pytest.mark.integration
async def test_reset_validates_fallback_and_disables_when_fallback_invalid(
    tmp_path: Path,
):
    database, _crypto, _tmdb, p115, _security, app = await _setup(tmp_path)
    fallback_path = tmp_path / "p115-cookie"
    fallback_path.write_text(COOKIE, encoding="ascii", newline="")
    if os.name != "nt":
        fallback_path.chmod(0o600)
    service = app.state.credential_service
    await service.update_p115_cookie(COOKIE, 0)
    await service.reset_p115_cookie(1)
    assert len(p115.calls) == 2
    p115.error = RuntimeError("fallback unavailable")
    snapshot = await service.reset_p115_cookie(2)
    assert snapshot["p115_cookie"]["ready"] is False  # type: ignore[index]
    await database.engine.dispose()


@pytest.mark.integration
async def test_callback_failure_fails_closed_after_persistence(tmp_path: Path):
    database, crypto, tmdb, p115, _security, _app = await _setup(tmp_path)
    fallback = CookieProvider(tmp_path / "fallback")
    provider = CompositeCookieProvider(fallback)
    states: list[bool] = []

    async def callback(ready: bool) -> None:
        states.append(ready)
        raise RuntimeError("worker failure")

    service = CredentialService(
        database.session_factory,
        crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=fallback,
        cookie_provider=provider,
        tmdb_client=tmdb,  # type: ignore[arg-type]
        p115_adapter=p115,  # type: ignore[arg-type]
        runtime_state=type("State", (), {})(),
        p115_runtime_callback=callback,
    )
    result = await service.update_p115_cookie(COOKIE, 0)
    assert result["p115_cookie"]["ready"] is False  # type: ignore[index]
    assert states == [True]
    await database.engine.dispose()


@pytest.mark.integration
async def test_cancel_during_post_commit_convergence_rethrows_after_finish(
    tmp_path: Path,
):
    database, crypto, tmdb, p115, _security, _app = await _setup(tmp_path)
    fallback = CookieProvider(tmp_path / "fallback")
    provider = CompositeCookieProvider(fallback)
    state = type("State", (), {"p115_ready": False, "push_capabilities": {}})()
    started = asyncio.Event()
    release = asyncio.Event()

    async def callback(_ready: bool) -> None:
        started.set()
        await release.wait()

    service = CredentialService(
        database.session_factory,
        crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=fallback,
        cookie_provider=provider,
        tmdb_client=tmdb,  # type: ignore[arg-type]
        p115_adapter=p115,  # type: ignore[arg-type]
        runtime_state=state,
        p115_runtime_callback=callback,
    )
    task = asyncio.create_task(service.update_p115_cookie(COOKIE, 0))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.p115_ready is True
    assert (await service.snapshot())["p115_cookie"]["source"] == "managed"  # type: ignore[index]
    await database.engine.dispose()


@pytest.mark.integration
async def test_cancelled_p115_validation_does_not_persist_candidate(tmp_path: Path):
    database, _crypto, _tmdb, p115, _security, app = await _setup(tmp_path)
    p115.block = asyncio.Event()
    task = asyncio.create_task(
        app.state.credential_service.update_p115_cookie(COOKIE, 0)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await app.state.credential_service.snapshot())["revision"] == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_rate_limit_and_rebuild_restore_managed_ciphertext(tmp_path: Path):
    database, crypto, _tmdb, _p115, security, app = await _setup(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://credentials.test"
    ) as client:
        csrf = await _login(client, security)
        saved = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": "persisted", "revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert saved.status_code == 200
        for _ in range(7):
            response = await client.put(
                "/api/v1/settings/credentials/tmdb",
                json={"value": "candidate", "revision": 99},
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 409
        limited = await client.put(
            "/api/v1/settings/credentials/tmdb",
            json={"value": "candidate", "revision": 99},
            headers={"X-CSRF-Token": csrf},
        )
        assert limited.status_code == 429
    await database.engine.dispose()

    fallback = CookieProvider(tmp_path / "rebuild-fallback")
    provider = CompositeCookieProvider(fallback)
    rebuilt_tmdb = FakeTmdb()
    rebuilt_p115 = FakeP115()
    rebuilt_database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}"
    )
    await initialize_database(rebuilt_database.engine)
    rebuilt = CredentialService(
        rebuilt_database.session_factory,
        crypto,
        environment_tmdb_key="environment",
        fallback_cookie_provider=fallback,
        cookie_provider=provider,
        tmdb_client=rebuilt_tmdb,  # type: ignore[arg-type]
        p115_adapter=rebuilt_p115,  # type: ignore[arg-type]
    )
    await rebuilt.load_managed()
    assert (await rebuilt.snapshot())["tmdb"]["source"] == "managed"  # type: ignore[index]
    await rebuilt_database.engine.dispose()
