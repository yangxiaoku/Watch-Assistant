import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.services.backups import BackupService, BackupServiceError


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def log_event(self, event: str, **kwargs: object) -> None:
        self.events.append({"event": event, **kwargs})


def _write_sample(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT)")
    connection.execute("DELETE FROM sample")
    connection.execute("INSERT INTO sample (value) VALUES (?)", (value,))
    connection.commit()
    connection.close()


def _read_sample(path: Path) -> str:
    connection = sqlite3.connect(path)
    value = connection.execute("SELECT value FROM sample").fetchone()[0]
    connection.close()
    return value


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'backup-api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, app


@pytest.mark.integration
async def test_backup_api_creates_consistent_snapshot_manifest_without_secrets(tmp_path):
    client, database, tmdb, pansou, app = await _make_client(tmp_path)
    try:
        created = await client.post("/api/v1/backups")
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

        preview = await client.get(f"/api/v1/backups/{body['backup_id']}/restore-preview")
        assert preview.status_code == 200
        assert preview.json()["status"] == "ready"
        assert preview.json()["sha256_valid"] is True
        assert preview.json()["integrity_ok"] is True
        assert "p115_cookie" in preview.json()["requires_reconfiguration"]
        assert preview.json()["warnings"] == ["restore_requires_service_stop_and_confirmation"]

        listed = await client.get("/api/v1/backups")
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


@pytest.mark.asyncio
async def test_backup_service_restores_verified_backup_and_cleans_sidecars(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()

    _write_sample(database_path, "after")
    (tmp_path / "source.db-wal").write_bytes(b"stale wal")
    (tmp_path / "source.db-shm").write_bytes(b"stale shm")

    result = await service.restore_to(
        backup.backup_id,
        database_path,
        confirmed=True,
        service_stopped=True,
    )

    assert result.status == "restored"
    assert result.integrity_ok is True
    assert result.restart_required is True
    assert result.pre_restore_backup_id != backup.backup_id
    assert _read_sample(database_path) == "before"
    assert not (tmp_path / "source.db-wal").exists()
    assert not (tmp_path / "source.db-shm").exists()


@pytest.mark.asyncio
async def test_backup_restore_requires_confirmation_and_service_stop(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "unchanged")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()

    with pytest.raises(BackupServiceError, match="confirmation_required"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=False,
            service_stopped=False,
        )
    assert _read_sample(database_path) == "unchanged"


@pytest.mark.asyncio
async def test_backup_restore_rejects_invalid_target_without_modifying_database(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "unchanged")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()

    with pytest.raises(BackupServiceError, match="restore_target_invalid"):
        await service.restore_to(
            backup.backup_id,
            tmp_path / "missing.db",
            confirmed=True,
            service_stopped=True,
        )
    assert _read_sample(database_path) == "unchanged"

    with pytest.raises(BackupServiceError, match="restore_requires_service_stop"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=False,
        )
    assert _read_sample(database_path) == "unchanged"


@pytest.mark.asyncio
async def test_backup_restore_rejects_digest_and_sqlite_tampering(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "unchanged")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()

    backup_path = tmp_path / "backups" / backup.file_name
    backup_path.write_bytes(b"tampered")
    with pytest.raises(BackupServiceError, match="backup_digest_mismatch"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )
    assert _read_sample(database_path) == "unchanged"

    corrupt = b"not a sqlite database"
    backup_path.write_bytes(corrupt)
    manifest_path = tmp_path / "backups" / f"{backup.backup_id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(corrupt).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupServiceError, match="backup_integrity_failed"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )
    assert _read_sample(database_path) == "unchanged"


@pytest.mark.asyncio
async def test_backup_restore_rejects_missing_current_migration(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY)"
    )
    connection.execute(
        "INSERT INTO schema_migrations (migration_id) VALUES ('999_current')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(BackupServiceError, match="restore_validation_failed"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )
    assert _read_sample(database_path) == "before"


@pytest.mark.asyncio
async def test_backup_restore_rolls_back_when_post_replace_validation_fails(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    _write_sample(database_path, "after")

    with patch(
        "watch_assistant.services.backups._sqlite_integrity_ok",
        side_effect=[True, False, True, True],
    ), pytest.raises(BackupServiceError, match="restore_validation_failed"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )

    assert _read_sample(database_path) == "after"
    assert len((await service.list()).items) == 2


@pytest.mark.asyncio
async def test_backup_restore_returns_stable_failure_after_replace_io_error(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    _write_sample(database_path, "after")
    original_copyfile = shutil.copyfile
    calls = 0

    def fail_first_copy(source: str | Path, target: str | Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated copy failure")
        return original_copyfile(source, target)

    with patch(
        "watch_assistant.services.backups.shutil.copyfile",
        side_effect=fail_first_copy,
    ), pytest.raises(BackupServiceError, match="restore_failed"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )

    assert _read_sample(database_path) == "after"
    assert calls == 2


@pytest.mark.asyncio
async def test_backup_restore_preserves_evidence_when_rollback_fails(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    _write_sample(database_path, "after")

    with patch(
        "watch_assistant.services.backups._sqlite_integrity_ok",
        side_effect=[True, False, False],
    ), pytest.raises(BackupServiceError, match="restore_rollback_failed"):
        await service.restore_to(
            backup.backup_id,
            database_path,
            confirmed=True,
            service_stopped=True,
        )

    assert _read_sample(database_path) == "before"
    assert len((await service.list()).items) == 2


@pytest.mark.asyncio
async def test_backup_restore_events_are_redacted(tmp_path):
    database_path = tmp_path / "secret-cookie-path.db"
    _write_sample(database_path, "unchanged")
    recorder = _EventRecorder()
    service = BackupService(
        str(database_path), tmp_path / "backups", event_logger=recorder
    )

    with pytest.raises(BackupServiceError, match="invalid_backup_id"):
        await service.restore_to(
            "../secret-cookie-path.db",
            database_path,
            confirmed=True,
            service_stopped=True,
        )

    encoded = json.dumps(recorder.events, ensure_ascii=False)
    assert "secret-cookie-path.db" not in encoded
    assert recorder.events[0]["event"] == "backup.restore_failed"
    assert recorder.events[0]["resource_id"] is None
