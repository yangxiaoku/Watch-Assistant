import asyncio
import json
import sqlite3
from pathlib import Path

from scripts.restore_backup import main
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
