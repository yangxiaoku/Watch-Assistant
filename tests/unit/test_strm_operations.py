import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from watch_assistant.api.strm import _cancel_operation
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import StrmOperation
from watch_assistant.services.strm_operations import (
    StrmOperationKind,
    StrmOperationService,
)


@pytest.mark.asyncio
async def test_strm_operation_lifecycle_is_durable_and_terminal(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        queued = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
        )
        assert queued.status == "queued"

        running = await service.start(queued.operation_id)
        assert running.status == "running"
        completed = await service.complete(
            queued.operation_id,
            generated=2,
            unchanged=1,
            skipped=0,
            failed=0,
            retired=0,
        )
        assert completed.status == "succeeded"
        assert completed.generated == 2

        repeated = await service.fail(
            queued.operation_id,
            error_code="late_failure",
            failed=1,
        )
        assert repeated.status == "succeeded"
        assert repeated.failed == 0

        items, next_cursor = await service.list("library-one", limit=1)
        assert [item.operation_id for item in items] == [queued.operation_id]
        assert next_cursor is None
        persisted = await service.get(queued.operation_id)
        assert persisted.status == "succeeded"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_strm_operation_failure_is_redacted_and_list_is_bounded(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        first = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind="incremental",
        )
        second = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-two",
            kind="incremental",
        )
        failed = await service.fail(
            first.operation_id,
            error_code="strm_operation_failed",
        )
        assert failed.status == "failed"
        assert failed.error_code == "strm_operation_failed"

        items, next_cursor = await service.list("library-one", limit=1)
        assert len(items) == 1
        assert items[0].operation_id == second.operation_id
        assert next_cursor is not None
        assert next_cursor != "1"

        decoded_page, final_cursor = await service.list(
            "library-one", cursor=next_cursor, limit=1
        )
        assert [item.operation_id for item in decoded_page] == [first.operation_id]
        assert final_cursor is None
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_strm_operation_recovery_terminalizes_stale_and_orphaned_rows(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        stale = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-stale",
            kind=StrmOperationKind.INCREMENTAL,
        )
        await service.start(stale.operation_id)
        queued = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-queued",
            kind=StrmOperationKind.FULL,
        )
        now = datetime.now(UTC)
        async with database.session_factory() as session:
            stale_row = await session.get(StrmOperation, stale.operation_id)
            assert stale_row is not None
            stale_row.updated_at = now - timedelta(hours=1)
            stale_row.lease_expires_at = now - timedelta(seconds=1)
            await session.commit()

        recovered = await service.recover_stale(
            max_age=timedelta(minutes=30), now=now
        )
        assert recovered == 1
        assert (await service.get(stale.operation_id)).error_code == "strm_operation_timeout"
        assert (await service.get(queued.operation_id)).status == "queued"

        recovered_orphan = await service.recover_incomplete(
            now=now, error_code="strm_operation_recovered"
        )
        assert recovered_orphan == 1
        orphan = await service.get(queued.operation_id)
        assert orphan.status == "failed"
        assert orphan.error_code == "strm_operation_recovered"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_strm_recovery_uses_cas_and_heartbeat_lease(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-heartbeat",
            kind=StrmOperationKind.FULL,
        )
        running = await service.start(
            operation.operation_id,
            now=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            lease_duration=timedelta(minutes=5),
        )
        assert running.status == "running"

        heartbeat = await service.heartbeat(
            operation.operation_id,
            now=datetime(2026, 8, 1, 0, 29, tzinfo=UTC),
            lease_duration=timedelta(minutes=5),
        )
        assert heartbeat.status == "running"

        assert await service.recover_stale(
            max_age=timedelta(minutes=30),
            now=datetime(2026, 8, 1, 0, 30, tzinfo=UTC),
        ) == 0

        async with database.session_factory() as session:
            row = await session.get(StrmOperation, operation.operation_id)
            assert row is not None
            observed_updated_at = row.updated_at
            row.status = "succeeded"
            row.finished_at = datetime(2026, 8, 1, 0, 30, 1, tzinfo=UTC)
            await session.commit()

        assert await service._recover(
            current_time=datetime(2026, 8, 1, 0, 31, tzinfo=UTC),
            error_code="strm_operation_timeout",
            cutoff=observed_updated_at,
            respect_lease=True,
        ) == 0
        assert (await service.get(operation.operation_id)).status == "succeeded"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_strm_timeout_and_cancelled_are_distinct_terminal_states(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        timeout_operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-timeout",
            kind=StrmOperationKind.FULL,
        )
        await service.start(timeout_operation.operation_id)
        cancelled_operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-cancelled",
            kind=StrmOperationKind.FULL,
        )
        await service.start(cancelled_operation.operation_id)

        now = datetime.now(UTC)
        async with database.session_factory() as session:
            row = await session.get(StrmOperation, timeout_operation.operation_id)
            assert row is not None
            row.lease_expires_at = now - timedelta(seconds=1)
            await session.commit()

        assert await service.recover_stale(max_age=timedelta(minutes=30), now=now) == 1
        assert (await service.get(timeout_operation.operation_id)).status == "timeout"
        assert (await service.cancel(cancelled_operation.operation_id)).status == "cancelled"
        resumed = await service.resume(cancelled_operation.operation_id)
        assert resumed.status == "queued"
        restarted = await service.start(cancelled_operation.operation_id)
        assert restarted.status == "running"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_operation_claim_start_fences_competing_resumers(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-claim",
            kind=StrmOperationKind.FULL,
        )
        await service.cancel(operation.operation_id)
        resumed = await service.resume(operation.operation_id)
        assert resumed.status == "queued"

        claims = await asyncio.gather(
            service.claim_start(operation.operation_id),
            service.claim_start(operation.operation_id),
        )

        assert sum(acquired for _, acquired in claims) == 1
        assert {summary.status for summary, _ in claims} == {"running"}
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_keyset_cursor_ignores_newer_insert_between_pages(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        oldest = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-oldest",
            kind=StrmOperationKind.FULL,
        )
        middle = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-middle",
            kind=StrmOperationKind.FULL,
        )
        async with database.session_factory() as session:
            oldest_row = await session.get(StrmOperation, oldest.operation_id)
            middle_row = await session.get(StrmOperation, middle.operation_id)
            assert oldest_row is not None
            assert middle_row is not None
            oldest_row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
            middle_row.created_at = datetime(2026, 1, 2, tzinfo=UTC)
            await session.commit()

        first_page, cursor = await service.list("library-one", limit=1)
        assert [item.operation_id for item in first_page] == [middle.operation_id]
        assert cursor is not None

        newest = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-newest",
            kind=StrmOperationKind.INCREMENTAL,
        )
        async with database.session_factory() as session:
            newest_row = await session.get(StrmOperation, newest.operation_id)
            assert newest_row is not None
            newest_row.created_at = datetime(2026, 1, 3, tzinfo=UTC)
            await session.commit()

        second_page, next_cursor = await service.list(
            "library-one", cursor=cursor, limit=1
        )
        assert [item.operation_id for item in second_page] == [oldest.operation_id]
        assert newest.operation_id not in {
            item.operation_id for item in first_page + second_page
        }
        assert next_cursor is None
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_operation_commits_after_repeated_request_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingOperations:
        committed = False

        async def fail(self, operation_id, *, error_code):
            started.set()
            await release.wait()
            self.committed = True
            return SimpleNamespace(status="failed")

    operations = BlockingOperations()
    cancellation_task = asyncio.create_task(
        _cancel_operation(
            None,  # workflow_id is absent, so no request state is needed
            operations,
            "strm_op_cancelled",
            workflow_id=None,
        )
    )
    await started.wait()
    cancellation_task.cancel()
    cancellation_task.cancel()
    release.set()

    await asyncio.wait_for(cancellation_task, timeout=1)
    assert operations.committed is True
