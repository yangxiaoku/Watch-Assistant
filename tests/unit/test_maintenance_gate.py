import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource
from watch_assistant.schemas import ResourceKind
from watch_assistant.services.backups import BackupService, BackupServiceError
from watch_assistant.services.maintenance_gate import (
    MaintenanceActive,
    MaintenanceDrainIncomplete,
    MaintenanceGate,
)
from watch_assistant.services.tasks import TaskService


@pytest.mark.asyncio
async def test_maintenance_mode_blocks_new_tasks(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    gate = MaintenanceGate(database.session_factory)
    service = TaskService(database.session_factory, maintenance_gate=gate)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="resource_test",
                kind=ResourceKind.MAGNET,
                canonical_key="magnet:?xt=urn:btih:" + "a" * 40,
                encrypted_url="encrypted",
                name="test",
                source="test",
                captured_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await session.commit()

    await gate.enter(reason="test")
    with pytest.raises(MaintenanceActive):
        await service.create("resource_test")

    status = await gate.status()
    assert status.active is True
    assert status.safe_point is True
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_restore_approval_requires_two_actors_and_safe_point(tmp_path):
    database_path = tmp_path / "watch.db"
    database = create_database(f"sqlite+aiosqlite:///{database_path}")
    await initialize_database(database.engine)
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.commit()
    connection.close()
    gate = MaintenanceGate(database.session_factory)
    service = BackupService(
        str(database_path),
        tmp_path / "backups",
        session_factory=database.session_factory,
        maintenance_gate=gate,
    )
    backup = await service.create()

    approval = await service.request_restore_approval(
        backup.backup_id, confirmed=True, requester_identity="session:first"
    )
    assert approval.status == "pending"
    assert approval.safe_point is True

    with pytest.raises(BackupServiceError, match="restore_second_approver_required"):
        await service.approve_restore(
            approval.approval_id,
            confirmed=True,
            approver_identity="session:first",
        )
    approved = await service.approve_restore(
        approval.approval_id,
        confirmed=True,
        approver_identity="session:second",
    )
    assert approved.status == "approved"
    assert approved.offline_restore_required is True
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_restore_approval_reports_non_terminal_tasks(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    gate = MaintenanceGate(database.session_factory)
    await gate.enter(reason="test")
    async with database.session_factory() as session:
        from watch_assistant.models import Task, TaskAction, TaskState

        session.add(
            Task(
                id="task_pending",
                action=TaskAction.OFFLINE_DOWNLOAD,
                encrypted_url_snapshot="encrypted",
                state=TaskState.QUEUED,
                attempts=0,
            )
        )
        await session.commit()
    with pytest.raises(MaintenanceDrainIncomplete):
        await gate.require_safe_point()
    status = await gate.status()
    assert status.safe_point is False
    assert "tasks_not_terminal" in status.blocking_reasons
    await database.engine.dispose()
