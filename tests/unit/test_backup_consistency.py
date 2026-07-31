import sqlite3

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.services.backups import BackupService


@pytest.mark.asyncio
async def test_business_consistency_rejects_orphaned_task_reference(tmp_path):
    database_path = tmp_path / "watch.db"
    database = create_database(f"sqlite+aiosqlite:///{database_path}")
    await initialize_database(database.engine)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO tasks "
        "(id, resource_id, action, encrypted_url_snapshot, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("task_orphan", "resource_missing", "offline_download", "encrypted", "queued", 0),
    )
    connection.commit()
    connection.close()

    service = BackupService(str(database_path), tmp_path / "backups")
    backup = await service.create()
    preview = await service.preview_restore(backup.backup_id)

    assert preview.status == "invalid"
    assert preview.business_consistency_ok is False
    failed = {
        item["name"]
        for item in preview.consistency_checks
        if item["status"] == "failed"
    }
    assert "tasks_resource" in failed
    await database.engine.dispose()
