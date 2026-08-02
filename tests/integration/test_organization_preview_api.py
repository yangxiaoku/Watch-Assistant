import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.schemas import MediaType
from watch_assistant.security import SecurityManager
from watch_assistant.services.media_matcher import MediaKind, TmdbCandidate

WEB_PASSWORD = "organization-preview-password"


class _FakeClient:
    async def aclose(self):
        return None

    async def search_candidates(self, _query):
        return [
            TmdbCandidate(
                tmdb_id=42,
                media_type=MediaType.MOVIE,
                title="The Office",
                kind=MediaKind.MOVIE,
                release_year=2005,
                origin_countries=("US",),
            )
        ]


@pytest.mark.integration
async def test_organization_preview_api_is_local_idempotent_and_redacted(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'preview-api.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-preview-api",
                name="Preview API",
                root_directory_id="root-preview-api",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id="scan-preview-api",
                library_id="library-preview-api",
                root_directory_id="root-preview-api",
                idempotency_key="preview-api-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=1,
                pages_read=1,
                items_seen=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-preview-api",
                object_type="file",
                object_id="file-preview-api",
                parent_id="root-preview-api",
                name="The.Office.2005.1080p.mkv",
                path="incoming/The.Office.2005.1080p.mkv",
                is_directory=False,
            )
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-preview-api",
                page=1,
                items_seen=1,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-preview-api": 1},
                        "expected_total": 1,
                        "pending": [],
                        "visited": ["root-preview-api"],
                    }
                ),
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
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
        frontend_dir=tmp_path / "missing",
        organization_plan_enabled=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        first = await client.post(
            "/api/v1/libraries/library-preview-api/organization-preview",
            headers=headers,
            json={
                "source_scan_run_id": "scan-preview-api",
                "source_directory_id": "root-preview-api",
            },
        )
        second = await client.post(
            "/api/v1/libraries/library-preview-api/organization-preview",
            headers=headers,
            json={"source_scan_run_id": "scan-preview-api"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] == second.json()["plan_id"]
    assert first.json()["status"] == "needs_review"
    assert "incoming/The.Office" not in first.text
    await database.engine.dispose()
