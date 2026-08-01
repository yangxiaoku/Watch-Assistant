import asyncio
import secrets
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import ApplicationSettings
from watch_assistant.services.prowlarr_settings import (
    ProwlarrSettingsConflict,
    ProwlarrSettingsRejected,
    ProwlarrSettingsService,
)


class _FakeSearchService:
    def __init__(self) -> None:
        self.clients = []

    async def replace_prowlarr_client(self, client) -> None:
        self.clients.append(client)


class _CommitSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.committed = False

    async def commit(self) -> None:
        self.started.set()
        await self.release.wait()
        self.committed = True


@pytest.mark.asyncio
async def test_commit_propagates_cancellation_after_commit_finishes():
    service = ProwlarrSettingsService.__new__(ProwlarrSettingsService)
    session = _CommitSession()
    task = asyncio.create_task(service._commit_uncancellable(session))

    await session.started.wait()
    task.cancel()
    session.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.committed is True


@pytest.mark.asyncio
async def test_prowlarr_settings_are_encrypted_and_fall_back_after_reset(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    fake_search = _FakeSearchService()
    environment_key = secrets.token_urlsafe(24)
    managed_key = secrets.token_urlsafe(24)
    service = ProwlarrSettingsService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
        environment_enabled=True,
        environment_base_url="http://env-prowlarr.test/",
        environment_api_key=environment_key,
        runtime_state=SimpleNamespace(search_service=fake_search),
    )

    initial = await service.snapshot()
    managed = await service.update(
        enabled=True,
        base_url="http://managed-prowlarr.test/prowlarr/",
        api_key=managed_key,
        fields_set={"enabled", "base_url", "api_key"},
        revision=initial["revision"],
    )

    assert managed["source"] == "managed"
    assert managed["api_key_source"] == "managed"
    assert managed["api_key_configured"] is True
    assert managed["base_url"] == "http://managed-prowlarr.test/prowlarr"
    assert managed_key not in str(managed)
    assert len(fake_search.clients) == 1
    async with database.session_factory() as session:
        stored = await session.scalar(select(ApplicationSettings))
    assert stored.managed_prowlarr_api_key_encrypted
    assert managed_key not in stored.managed_prowlarr_api_key_encrypted

    fallback = await service.reset(managed["revision"])
    assert fallback["source"] == "environment"
    assert fallback["base_url"] == "http://env-prowlarr.test"
    assert fallback["api_key_source"] == "environment"
    assert len(fake_search.clients) == 2

    await database.engine.dispose()


@pytest.mark.asyncio
async def test_prowlarr_settings_reject_unsafe_url_and_stale_revision(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    service = ProwlarrSettingsService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
    )

    snapshot = await service.snapshot()
    api_key = secrets.token_urlsafe(24)
    with pytest.raises(ProwlarrSettingsRejected):
        await service.update(
            enabled=True,
            base_url="https://user:password@prowlarr.test/search?key=secret",
            api_key=api_key,
            fields_set={"enabled", "base_url", "api_key"},
            revision=snapshot["revision"],
        )
    with pytest.raises(ProwlarrSettingsConflict):
        await service.update(
            enabled=True,
            base_url="http://prowlarr.test",
            api_key=api_key,
            fields_set={"enabled", "base_url", "api_key"},
            revision=99,
        )

    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_commit_still_applies_persisted_runtime_state(tmp_path, monkeypatch):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    fake_search = _FakeSearchService()
    service = ProwlarrSettingsService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
        runtime_state=SimpleNamespace(search_service=fake_search),
    )
    initial = await service.snapshot()

    async def commit_then_report_cancelled(session) -> None:
        await session.commit()
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_commit_uncancellable", commit_then_report_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await service.update(
            enabled=True,
            base_url="http://managed-prowlarr.test",
            api_key=secrets.token_urlsafe(24),
            fields_set={"enabled", "base_url", "api_key"},
            revision=initial["revision"],
        )

    snapshot = await service.snapshot()
    assert snapshot["source"] == "managed"
    assert len(fake_search.clients) == 1
    await fake_search.clients[0].aclose()
    await database.engine.dispose()
