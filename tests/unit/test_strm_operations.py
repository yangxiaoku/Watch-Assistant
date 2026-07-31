from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary
from watch_assistant.models import StrmOperationStatus
from watch_assistant.services.strm_operations import (
    StrmOperationError,
    StrmOperationNotFound,
    StrmOperationService,
)


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm-operations.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-strm",
                name="STRM",
                root_directory_id="root-strm",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()
    return database


@pytest.mark.asyncio
async def test_operation_lifecycle_persists_stats_and_is_readable(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmOperationService(database.session_factory)
        queued = await service.create(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            kind="full",
        )
        assert queued.status == StrmOperationStatus.QUEUED.value
        running = await service.start(queued.operation_id)
        assert running.status == StrmOperationStatus.RUNNING.value
        completed = await service.complete(
            queued.operation_id,
            generated=3,
            unchanged=2,
            skipped=1,
            failed=0,
            retired=4,
        )

        assert completed.status == StrmOperationStatus.SUCCEEDED.value
        assert completed.generated == 3
        assert completed.retired == 4
        assert completed.started_at is not None
        assert completed.finished_at is not None
        loaded = await service.get(queued.operation_id)
        assert loaded == completed
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_failure_is_idempotent_and_success_cannot_be_overwritten(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmOperationService(database.session_factory)
        failed = await service.create(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            kind="cleanup",
        )
        first = await service.fail(
            failed.operation_id,
            error_code="source_snapshot_not_ready",
            skipped=2,
        )
        repeated = await service.fail(
            failed.operation_id,
            error_code="different_error",
            failed=4,
        )
        assert repeated == first
        assert repeated.status == StrmOperationStatus.FAILED.value
        assert repeated.error_code == "source_snapshot_not_ready"
        assert repeated.skipped == 2

        succeeded = await service.create(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            kind="incremental",
        )
        success = await service.complete(
            succeeded.operation_id,
            generated=1,
            unchanged=0,
            skipped=0,
            failed=0,
            retired=0,
        )
        late_failure = await service.fail(
            succeeded.operation_id,
            error_code="late_error",
            failed=1,
        )
        assert late_failure == success
        assert late_failure.status == StrmOperationStatus.SUCCEEDED.value
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_operation_migration_is_present_and_idempotent(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        async with database.engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "strm_operations" in tables
        await initialize_database(database.engine)
        async with database.engine.connect() as connection:
            migration_id = await connection.scalar(
                text(
                    "SELECT migration_id FROM schema_migrations "
                    "WHERE migration_id = '053_strm_operations'"
                )
            )
        assert migration_id == "053_strm_operations"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_missing_operation_has_stable_error(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        with pytest.raises(StrmOperationNotFound):
            await StrmOperationService(database.session_factory).get("missing")
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_operation_history_is_library_scoped_and_cursor_paginated(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmOperationService(database.session_factory)
        for kind in ("full", "incremental", "cleanup"):
            await service.create(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                kind=kind,
            )
        first, cursor = await service.list("library-strm", limit=2)
        assert len(first) == 2
        assert cursor == 2
        second, next_cursor = await service.list("library-strm", cursor=cursor, limit=2)
        assert len(second) == 1
        assert next_cursor is None
        with pytest.raises(StrmOperationError, match="invalid_cursor"):
            await service.list("library-strm", cursor=-1)
    finally:
        await database.engine.dispose()
