from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryHealthReport,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
)
from watch_assistant.models import Task, TaskState
from watch_assistant.schemas import TaskAction
from watch_assistant.services.library_health_service import LibraryHealthService


class _EventLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def log_event(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


@pytest.mark.asyncio
async def test_health_report_is_persisted_idempotently_and_notifies_severe_issues(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-health",
                name="Health fixture",
                root_directory_id="root-health",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-health",
                library_id="library-health",
                root_directory_id="root-health",
                idempotency_key="health-scan",
                state="completed",
                complete=True,
                snapshot_revision=3,
                pages_read=1,
                items_seen=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-health",
                object_type="file",
                object_id="file-health",
                parent_id="root-health",
                name="fixture.mkv",
                is_directory=False,
                modified_at=now,
            )
        )
        session.add(
            StrmManifestEntry(
                manifest_id="manifest-health",
                library_id="library-health",
                cloud_file_id="file-health",
                cloud_relative_path="fixture.mkv",
                local_relative_path="fixture.strm",
                source_version=3,
                status="pending",
                is_current=True,
            )
        )
        session.add(
            Task(
                id="task-health",
                target_directory_id="root-health",
                action=TaskAction.OFFLINE_DOWNLOAD,
                encrypted_url_snapshot="encrypted",
                state=TaskState.UNCERTAIN,
            )
        )
        await session.commit()

    event_logger = _EventLogger()
    service = LibraryHealthService(
        database.session_factory,
        event_logger=event_logger,
    )
    first = await service.run("library-health")
    second = await service.run("library-health")
    latest = await service.latest("library-health")

    assert first.report_id == second.report_id == latest.report_id
    assert first.report.issue_counts["error"] == 2
    assert [event[0] for event in event_logger.events] == ["library.health.critical"]
    async with database.session_factory() as session:
        assert len((await session.scalars(select(LibraryHealthReport))).all()) == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_inventory_does_not_notify_inventory_dependent_orphan(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'partial-health.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-partial",
                name="Partial fixture",
                root_directory_id="root-partial",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-partial",
                library_id="library-partial",
                root_directory_id="root-partial",
                idempotency_key="partial-scan",
                state="failed",
                complete=False,
                snapshot_revision=None,
            )
        )
        session.add(
            StrmManifestEntry(
                manifest_id="manifest-partial",
                library_id="library-partial",
                cloud_file_id="missing-from-partial-scan",
                cloud_relative_path="fixture.mkv",
                local_relative_path="fixture.strm",
                source_version=1,
                status="verified",
                is_current=True,
            )
        )
        await session.commit()

    event_logger = _EventLogger()
    result = await LibraryHealthService(
        database.session_factory,
        event_logger=event_logger,
    ).run("library-partial")

    assert result.report.inventory_complete is False
    assert any(issue.requires_complete_inventory for issue in result.report.issues)
    assert event_logger.events == []
    await database.engine.dispose()
