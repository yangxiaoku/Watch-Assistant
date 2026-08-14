"""库存重复检测 API 集成测试。"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'inventory_audit_api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=make_security_manager(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, app


async def _seed(database, *, verified=True):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="lib_1",
                name="整理归档",
                root_directory_id="root_1",
                scope_verified=verified,
                enabled=True,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="run_1",
                library_id="lib_1",
                root_directory_id="root_1",
                idempotency_key="k1",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="run_1",
                    object_type="file",
                    object_id="f1",
                    name="某剧.S01E01.1080p.mkv",
                    is_directory=False,
                    size_bytes=1000,
                ),
                LibraryScanEntry(
                    scan_run_id="run_1",
                    object_type="file",
                    object_id="f2",
                    name="某剧.S01E01.1080p.mkv",
                    is_directory=False,
                    size_bytes=1000,
                ),
            ]
        )
        await session.commit()


@pytest.mark.integration
async def test_inventory_audit_returns_report(tmp_path):
    client, database, app = await _make_client(tmp_path)
    await _seed(database)
    app.state.organization_target_root_id = "root_1"
    try:
        resp = await client.get("/api/v1/inventory/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["duplicate_count"] == 1
        assert body["reclaimable_bytes"] == 1000
        assert body["groups"][0]["kind"] == "exact_duplicate"
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_inventory_audit_unconfigured_fails_closed(tmp_path):
    client, _database, _app = await _make_client(tmp_path)
    # 未设置 organization_target_root_id
    try:
        resp = await client.get("/api/v1/inventory/audit")
        assert resp.status_code == 503
    finally:
        await client.aclose()
