from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
)
from watch_assistant.models import Notification
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "library-health-api-password"


class _FakeClient:
    async def aclose(self):
        return None


async def _client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'library-health-api.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-health-api",
                name="Health API fixture",
                root_directory_id="root-health-api",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-health-api",
                library_id="library-health-api",
                root_directory_id="root-health-api",
                idempotency_key="health-api-scan",
                state="completed",
                complete=True,
                snapshot_revision=8,
                pages_read=1,
                items_seen=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-health-api",
                object_type="file",
                object_id="file-health-api",
                parent_id="root-health-api",
                name="fixture.mkv",
                is_directory=False,
            )
        )
        session.add(
            StrmManifestEntry(
                manifest_id="manifest-health-api",
                library_id="library-health-api",
                cloud_file_id="file-health-api",
                cloud_relative_path="fixture.mkv",
                local_relative_path="fixture.strm",
                source_version=8,
                status="pending",
                is_current=True,
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(WEB_PASSWORD),
            script_token_hash=password_hash.hash("unused-script-credential"),
        ),
        frontend_dir=tmp_path / "missing",
    )
    return app, database


@pytest.mark.integration
async def test_health_api_persists_report_and_notifies_once_for_idempotent_run(tmp_path):
    app, database = await _client(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        first = await client.post(
            "/api/v1/libraries/library-health-api/health-check",
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["source_scan_run_id"] == "scan-health-api"
        assert first.json()["issue_counts"]["error"] == 1
        assert first.json()["issues"][0]["title_zh"] == "STRM 内容无效"

        second = await client.post(
            "/api/v1/libraries/library-health-api/health-check",
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["report_id"] == first.json()["report_id"]

        latest = await client.get(
            "/api/v1/libraries/library-health-api/health",
            headers=headers,
        )
        assert latest.status_code == 200
        assert latest.json()["report_id"] == first.json()["report_id"]

    async with database.session_factory() as session:
        notifications = list(
            (
                await session.scalars(
                    select(Notification).where(
                        Notification.event_code == "library.health.critical"
                    )
                )
            ).all()
        )
    assert len(notifications) == 1
    assert notifications[0].action_type == "library"
    assert notifications[0].action_id == "library-health-api"
    await database.engine.dispose()
