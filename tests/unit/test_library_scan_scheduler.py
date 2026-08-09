from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import LibraryScanRun, MediaLibrary
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
)
from watch_assistant.services.library_scan_scheduler import LibraryScanScheduler

LIBRARY_ID = "main"
ROOT_ID = "2988794667098701570"


async def _make_env(tmp_path, *, enabled=True, scope_verified=True):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="library",
                root_directory_id=ROOT_ID,
                scope_verified=scope_verified,
                enabled=enabled,
            )
        )
        await session.commit()
    operations = LibraryScanOperationService(database.session_factory)
    scheduler = LibraryScanScheduler(database.session_factory, operations)
    return database, scheduler


@pytest.mark.asyncio
async def test_scheduler_enqueues_one_scan_per_library_per_day(tmp_path):
    database, scheduler = await _make_env(tmp_path)
    try:
        result = await scheduler.run_due_once(now=datetime(2026, 8, 9, tzinfo=UTC))
        assert result.libraries == 1
        assert result.scanned == 1
        assert result.failed == 0

        # A second tick the same day must not enqueue a duplicate scan.
        result = await scheduler.run_due_once(now=datetime(2026, 8, 9, tzinfo=UTC))
        assert result.scanned == 0
        assert result.skipped == 1

        async with database.session_factory() as session:
            runs = list(await session.scalars(select(LibraryScanRun)))
        assert len(runs) == 1
        assert runs[0].idempotency_key == "scheduled-2026-08-09"

        # A new UTC day enqueues a fresh scan with the new date key.
        result = await scheduler.run_due_once(now=datetime(2026, 8, 10, tzinfo=UTC))
        assert result.scanned == 1
        async with database.session_factory() as session:
            runs = list(await session.scalars(select(LibraryScanRun)))
        assert len(runs) == 2
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_skips_disabled_or_unverified_libraries(tmp_path):
    database, scheduler = await _make_env(tmp_path, enabled=False)
    try:
        result = await scheduler.run_due_once(now=datetime(2026, 8, 9, tzinfo=UTC))
        assert result.libraries == 0
        assert result.scanned == 0
    finally:
        await database.engine.dispose()
