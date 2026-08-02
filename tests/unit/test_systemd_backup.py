import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_backup_script():
    path = ROOT / "scripts" / "systemd_backup.py"
    spec = importlib.util.spec_from_file_location("systemd_backup", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = _load_backup_script()
COMMIT = "c" * 40


def _write_database(path: Path, value: str = "before") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sample (value TEXT NOT NULL);
        CREATE TABLE schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
    connection.execute("INSERT INTO schema_migrations(migration_id) VALUES ('001')")
    connection.commit()
    connection.close()


def test_systemd_backup_is_atomic_and_manifest_is_verified(tmp_path: Path):
    database = tmp_path / "watch-assistant.db"
    output_dir = tmp_path / "backups"
    _write_database(database)

    manifest = backup.create_backup(
        database=database,
        output_dir=output_dir,
        release=COMMIT,
    )
    manifest_path = output_dir / f"{manifest['backup_id']}.json"
    database_path = output_dir / str(manifest["database_file"])
    assert database_path.is_file()
    assert manifest_path.is_file()
    assert backup._sha256(database_path) == manifest["sha256"]
    assert backup.inspect_backup(manifest=manifest_path)["status"] == "ready"
    assert not list(output_dir.glob(".*.tmp"))

    database_path.write_bytes(b"tampered")
    preview = backup.inspect_backup(manifest=manifest_path)
    assert preview["status"] == "invalid"
    assert preview["integrity_ok"] is False


def test_systemd_backup_restore_requires_explicit_manual_confirmation(tmp_path: Path):
    database = tmp_path / "watch-assistant.db"
    output_dir = tmp_path / "backups"
    _write_database(database)
    manifest = backup.create_backup(
        database=database,
        output_dir=output_dir,
        release=COMMIT,
    )
    manifest_path = output_dir / f"{manifest['backup_id']}.json"

    with pytest.raises(backup.SystemdBackupError, match="restore_requires_explicit_confirmation"):
        backup.restore_backup(
            manifest=manifest_path,
            database=database,
            confirmed=False,
            service_stopped=True,
        )
    with pytest.raises(backup.SystemdBackupError, match="restore_requires_service_stop"):
        backup.restore_backup(
            manifest=manifest_path,
            database=database,
            confirmed=True,
            service_stopped=False,
        )

    connection = sqlite3.connect(database)
    connection.execute("UPDATE sample SET value = 'changed'")
    connection.commit()
    connection.close()
    result = backup.restore_backup(
        manifest=manifest_path,
        database=database,
        confirmed=True,
        service_stopped=True,
    )
    assert result["status"] == "restored"
    assert database.is_file()
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT value FROM sample").fetchone()[0] == "before"
    connection.close()
    assert (output_dir / str(manifest["database_file"])).is_file()


def test_systemd_backup_rejects_unknown_release(tmp_path: Path):
    database = tmp_path / "watch-assistant.db"
    _write_database(database)
    with pytest.raises(backup.SystemdBackupError, match="full_release_required"):
        backup.create_backup(
            database=database,
            output_dir=tmp_path / "backups",
            release="unknown",
        )
