import asyncio
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from scripts.restore_backup import main
from watch_assistant.db import create_database, initialize_database
from watch_assistant.services.backups import BackupService


def _write_value(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample (value) VALUES (?)", (value,))
    connection.commit()
    connection.close()


def _read_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    value = connection.execute("SELECT value FROM sample").fetchone()[0]
    connection.close()
    return value


def _insert_approved_restore(path: Path, backup_id: str, sha256: str) -> str:
    approval_id = "restore_" + "a" * 32
    now = datetime.now(UTC)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO online_maintenance_state "
        "(id, active, reason, generation, requested_at, entered_at, requested_by) "
        "VALUES ('default', 1, 'database_restore', 1, ?, ?, 'session:first')",
        (now.isoformat(), now.isoformat()),
    )
    connection.execute(
        "INSERT INTO backup_restore_approvals "
        "(id, backup_id, backup_sha256, requester_identity, approver_identity, "
        "status, maintenance_generation, drain_report_json, created_at, expires_at, approved_at) "
        "VALUES (?, ?, ?, ?, ?, 'approved', 1, '{}', ?, ?, ?)",
        (
            approval_id,
            backup_id,
            sha256,
            "session:first",
            "session:second",
            now.isoformat(),
            (now + timedelta(minutes=5)).isoformat(),
            now.isoformat(),
        ),
    )
    connection.commit()
    connection.close()
    return approval_id


def test_restore_backup_script_requires_both_safety_flags(tmp_path, capsys):
    database_path = tmp_path / "database.db"
    _write_value(database_path, "original")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = asyncio.run(service.create())

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--backup-id",
            backup.backup_id,
            "--confirm",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 2
    assert payload["error_code"] == "restore_requires_service_stop"
    assert str(database_path) not in output
    assert _read_value(database_path) == "original"


def test_restore_backup_script_outputs_redacted_success(tmp_path, capsys):
    database_path = tmp_path / "database.db"
    _write_value(database_path, "original")
    service = BackupService(str(database_path), tmp_path / "backups")
    backup = asyncio.run(service.create())
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE sample SET value = 'changed'")
    connection.commit()
    connection.close()

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--backup-id",
            backup.backup_id,
            "--confirm",
            "--service-stopped",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert payload["status"] == "restored"
    assert payload["restart_required"] is True
    assert str(database_path) not in output
    assert str(tmp_path / "backups") not in output
    assert _read_value(database_path) == "original"


def test_restore_backup_script_requires_and_consumes_application_approval(
    tmp_path, capsys
):
    database_path = tmp_path / "database.db"
    database = create_database(f"sqlite+aiosqlite:///{database_path}")
    asyncio.run(initialize_database(database.engine))
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample (value) VALUES ('original')")
    connection.commit()
    connection.close()

    service = BackupService(str(database_path), tmp_path / "backups")
    backup = asyncio.run(service.create())
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE sample SET value = 'changed'")
    connection.commit()
    connection.close()

    missing = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--backup-id",
            backup.backup_id,
            "--confirm",
            "--service-stopped",
        ]
    )
    assert missing == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "restore_approval_required"

    approval_id = _insert_approved_restore(database_path, backup.backup_id, backup.sha256)
    restored = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--backup-id",
            backup.backup_id,
            "--approval-id",
            approval_id,
            "--confirm",
            "--service-stopped",
        ]
    )
    assert restored == 0
    assert json.loads(capsys.readouterr().out)["business_consistency_ok"] is True
    journal = tmp_path / "backups" / f".restore-approval-{approval_id}.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "consumed"

    repeated = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--backup-id",
            backup.backup_id,
            "--approval-id",
            approval_id,
            "--confirm",
            "--service-stopped",
        ]
    )
    assert repeated == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "restore_approval_conflict"
    asyncio.run(database.engine.dispose())


def test_restore_backup_script_restores_encrypted_copy_from_stdin(tmp_path, capsys, monkeypatch):
    database_path = tmp_path / "database.db"
    _write_value(database_path, "original")
    destination = tmp_path / "encrypted"
    service = BackupService(
        str(database_path),
        tmp_path / "backups",
        encrypted_backup_enabled=True,
        encrypted_backup_destination=destination,
    )
    backup = asyncio.run(service.create())
    recovery_key = Fernet.generate_key().decode("ascii")
    encrypted = asyncio.run(
        service.create_encrypted_copy(backup.backup_id, recovery_key=recovery_key)
    )
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE sample SET value = 'changed'")
    connection.commit()
    connection.close()
    monkeypatch.setattr("sys.stdin", io.StringIO(recovery_key + "\n"))

    exit_code = main(
        [
            "--database",
            str(database_path),
            "--backup-directory",
            str(tmp_path / "backups"),
            "--encrypted-manifest",
            str(destination / encrypted.manifest_file_name),
            "--recovery-key-stdin",
            "--confirm",
            "--service-stopped",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert payload["status"] == "restored"
    assert str(destination) not in output
    assert recovery_key not in output
    assert _read_value(database_path) == "original"
