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


async def _make_client(
    tmp_path: Path,
    *,
    encrypted_backup_enabled: bool = False,
    encrypted_backup_destination: Path | None = None,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'backup-api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        encrypted_backup_enabled=encrypted_backup_enabled,
        encrypted_backup_destination=encrypted_backup_destination,
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

        exported = await client.get("/api/v1/backups/configuration")
        assert exported.status_code == 200
        configuration = exported.json()
        assert configuration["schema_version"] == 1
        assert configuration["logging"]["level"] == "INFO"
        assert configuration["organization"]["schedule_enabled"] is False
        assert configuration["requires_reconfiguration"] == [
            "tmdb_api_key",
            "p115_cookie",
            "web_password",
            "agent_token",
        ]
        assert "managed_tmdb_key_encrypted" not in exported.text
        assert "managed_p115_cookie_encrypted" not in exported.text

        import_payload = configuration | {
            "expected_settings_revision": configuration["logging"]["revision"],
            "expected_notification_revision": configuration["notifications"]["revision"],
            "confirmed": True,
        }
        import_payload["logging"] = configuration["logging"] | {"retention_days": 20}
        import_payload["notifications"] = configuration["notifications"] | {"enabled": False}
        imported = await client.post(
            "/api/v1/backups/configuration/import", json=import_payload
        )
        assert imported.status_code == 200
        assert imported.json()["status"] == "imported"
        assert imported.json()["imported_sections"] == [
            "logging",
            "inspection",
            "content_policy",
            "organization",
            "notifications",
        ]
        assert imported.json()["requires_reconfiguration"] == [
            "tmdb_api_key",
            "p115_cookie",
            "web_password",
            "agent_token",
        ]
        assert (await client.get("/api/v1/settings/logging")).json()["retention_days"] == 20
        assert (await client.get("/api/v1/notification-preferences")).json()["enabled"] is False

        missing_confirmation = await client.post(
            "/api/v1/backups/configuration/import",
            json=import_payload | {"confirmed": False},
        )
        assert missing_confirmation.status_code == 422
        assert missing_confirmation.json()["error"]["code"] == "backup_configuration_confirmation_required"

        invalid = import_payload | {
            "expected_settings_revision": imported.json()["settings_revision"],
            "expected_notification_revision": imported.json()["notification_revision"],
            "organization": import_payload["organization"] | {
                "source_directory_ids": ["100"],
                "target_directory_id": "100",
            },
        }
        invalid_response = await client.post(
            "/api/v1/backups/configuration/import", json=invalid
        )
        assert invalid_response.status_code == 422
        assert invalid_response.json()["error"]["code"] == "backup_configuration_invalid"
        assert (await client.get("/api/v1/settings/logging")).json()["retention_days"] == 20

        conflict = await client.post(
            "/api/v1/backups/configuration/import",
            json=import_payload | {
                "expected_settings_revision": 0,
                "expected_notification_revision": 1,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "backup_configuration_conflict"

        listed = await client.get("/api/v1/backups")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["backup_id"] == body["backup_id"]

        log_items, _ = await app.state.settings_service.log_store.list(
            cursor=None, limit=50, category=None
        )
        event_codes = {item["event_code"] for item in log_items}
        assert {
            "backup.created",
            "backup.restore_preview",
            "backup.configuration_exported",
            "backup.configuration_imported",
        } <= event_codes
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
async def test_backup_service_requires_configuration_dependencies(tmp_path):
    service = BackupService(":memory:", tmp_path / "backups")
    with pytest.raises(
        BackupServiceError, match="backup_configuration_unavailable"
    ):
        await service.export_configuration()


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
async def test_backup_service_lists_validation_and_deletes_only_with_confirmation(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "value")
    service = BackupService(str(database_path), tmp_path / "backups", retention_count=3)
    first = await service.create()
    second = await service.create()

    listed = await service.list()
    assert listed.retention_count == 3
    assert [item.validation_status for item in listed.items] == ["verified", "verified"]
    assert listed.items[0].retention_rank == 1
    assert listed.items[1].retention_rank == 2

    with pytest.raises(BackupServiceError, match="backup_delete_confirmation_required"):
        await service.delete(first.backup_id, confirmed=False)
    deleted = await service.delete(first.backup_id, confirmed=True)
    assert deleted.status == "deleted"
    assert [item.backup_id for item in (await service.list()).items] == [second.backup_id]
    with pytest.raises(BackupServiceError, match="backup_delete_last"):
        await service.delete(second.backup_id, confirmed=True)


@pytest.mark.asyncio
async def test_backup_api_deletes_old_backup_and_protects_last_one(tmp_path):
    client, database, tmdb, pansou, _app = await _make_client(tmp_path)
    try:
        first = (await client.post("/api/v1/backups")).json()
        second = (await client.post("/api/v1/backups")).json()
        missing_confirmation = await client.request(
            "DELETE", f"/api/v1/backups/{first['backup_id']}", json={"confirmed": False}
        )
        assert missing_confirmation.status_code == 400
        assert missing_confirmation.json()["detail"] == "backup_delete_confirmation_required"

        deleted = await client.request(
            "DELETE", f"/api/v1/backups/{first['backup_id']}", json={"confirmed": True}
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"status": "deleted", "backup_id": first["backup_id"]}

        last = await client.request(
            "DELETE", f"/api/v1/backups/{second['backup_id']}", json={"confirmed": True}
        )
        assert last.status_code == 409
        assert last.json()["detail"] == "backup_delete_last"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_encrypted_backup_copy_is_explicit_verifiable_and_restorable(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "before")
    destination = tmp_path / "encrypted"
    service = BackupService(
        str(database_path),
        tmp_path / "backups",
        encrypted_backup_enabled=True,
        encrypted_backup_destination=destination,
    )
    backup = await service.create()
    recovery_key = Fernet.generate_key().decode("ascii")
    encrypted = await service.create_encrypted_copy(
        backup.backup_id, recovery_key=recovery_key
    )

    encrypted_path = destination / encrypted.file_name
    manifest_path = destination / encrypted.manifest_file_name
    assert encrypted.validation_status == "verified"
    assert encrypted_path.is_file()
    assert manifest_path.is_file()
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert recovery_key not in manifest_text
    assert b"before" not in encrypted_path.read_bytes()

    _write_sample(database_path, "after")
    restored = await service.restore_encrypted_to(
        manifest_path,
        database_path,
        recovery_key=recovery_key,
        confirmed=True,
        service_stopped=True,
    )
    assert restored.status == "restored"
    assert _read_sample(database_path) == "before"

    with pytest.raises(BackupServiceError, match="encrypted_backup_key_invalid"):
        await service.restore_encrypted_to(
            manifest_path,
            database_path,
            recovery_key=Fernet.generate_key().decode("ascii"),
            confirmed=True,
            service_stopped=True,
        )


@pytest.mark.asyncio
async def test_encrypted_backup_copy_is_disabled_by_default(tmp_path):
    database_path = tmp_path / "source.db"
    _write_sample(database_path, "value")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    with pytest.raises(BackupServiceError, match="encrypted_backup_disabled"):
        await service.create_encrypted_copy(
            backup.backup_id,
            recovery_key=Fernet.generate_key().decode("ascii"),
        )


@pytest.mark.asyncio
async def test_encrypted_backup_copy_api_never_returns_recovery_key(tmp_path):
    destination = tmp_path / "encrypted"
    client, database, tmdb, pansou, _app = await _make_client(
        tmp_path,
        encrypted_backup_enabled=True,
        encrypted_backup_destination=destination,
    )
    recovery_key = Fernet.generate_key().decode("ascii")
    try:
        backup = (await client.post("/api/v1/backups")).json()
        missing_confirmation = await client.post(
            f"/api/v1/backups/{backup['backup_id']}/encrypted-copy",
            json={"recovery_key": recovery_key, "confirmed": False},
        )
        assert missing_confirmation.status_code == 400
        created = await client.post(
            f"/api/v1/backups/{backup['backup_id']}/encrypted-copy",
            json={"recovery_key": recovery_key, "confirmed": True},
        )
        assert created.status_code == 201
        assert created.json()["validation_status"] == "verified"
        assert recovery_key not in created.text
        assert recovery_key not in (destination / created.json()["manifest_file_name"]).read_text(
            encoding="utf-8"
        )
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


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
