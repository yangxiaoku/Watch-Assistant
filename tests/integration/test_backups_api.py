import hashlib
import sqlite3
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import SecurityManager
from watch_assistant.services.backups import BackupService, BackupServiceError

BACKUP_PASSWORD = "backup-test-password"


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'backup-api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(BACKUP_PASSWORD),
            script_token_hash=password_hash.hash("script-token"),
            cookie_secure=False,
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    login = await client.post(
        "/api/v1/auth/login", json={"password": BACKUP_PASSWORD}
    )
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    return client, database, tmdb, pansou, app, headers


@pytest.mark.integration
async def test_backup_api_creates_consistent_snapshot_manifest_without_secrets(tmp_path):
    client, database, tmdb, pansou, app, headers = await _make_client(tmp_path)
    try:
        created = await client.post("/api/v1/backups", headers=headers)
        assert created.status_code == 201
        body = created.json()
        assert body["backup_id"].startswith("backup_")
        assert body["size_bytes"] > 0
        assert len(body["sha256"]) == 64
        assert "unused" not in created.text
        assert "Fernet" not in created.text

        backup_dir = app.state.backup_service._backup_directory
        database_path = backup_dir / body["file_name"]
        assert database_path.is_file()
        assert hashlib.sha256(database_path.read_bytes()).hexdigest() == body["sha256"]

        preview = await client.get(
            f"/api/v1/backups/{body['backup_id']}/restore-preview", headers=headers
        )
        assert preview.status_code == 200
        assert preview.json()["status"] == "ready"
        assert preview.json()["sha256_valid"] is True
        assert preview.json()["integrity_ok"] is True
        assert "p115_cookie" in preview.json()["requires_reconfiguration"]
        assert preview.json()["warnings"] == ["restore_requires_service_stop_and_confirmation"]

        listed = await client.get("/api/v1/backups", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["backup_id"] == body["backup_id"]

        log_items, _ = await app.state.settings_service.log_store.list(
            cursor=None, limit=50, category=None
        )
        event_codes = {item["event_code"] for item in log_items}
        assert {"backup.created", "backup.restore_preview"} <= event_codes
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_backup_service_rejects_memory_database(tmp_path):
    service = BackupService(":memory:", tmp_path / "backups")
    with pytest.raises(BackupServiceError, match="backup_requires_file_database"):
        await service.create()


@pytest.mark.asyncio
async def test_backup_service_applies_explicit_retention(tmp_path):
    database_path = tmp_path / "source.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.commit()
    connection.close()

    service = BackupService(
        str(database_path), tmp_path / "backups", retention_count=2
    )
    await service.create()
    await service.create()
    await service.create()

    listed = await service.list()
    assert len(listed.items) == 2
    assert len(list((tmp_path / "backups").glob("backup_*.db"))) == 2


@pytest.mark.asyncio
async def test_backup_restore_preview_rejects_digest_tampering(tmp_path):
    database_path = tmp_path / "source.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.commit()
    connection.close()

    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    (tmp_path / "backups" / backup.file_name).write_bytes(b"tampered")

    preview = await service.preview_restore(backup.backup_id)
    assert preview.status == "invalid"
    assert preview.sha256_valid is False
    assert preview.integrity_ok is False


@pytest.mark.asyncio
async def test_backup_restore_preview_rejects_unsafe_id(tmp_path):
    service = BackupService(str(tmp_path / "source.db"), tmp_path / "backups")
    with pytest.raises(BackupServiceError, match="invalid_backup_id"):
        await service.preview_restore("../backup")
