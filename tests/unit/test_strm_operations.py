import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import watch_assistant.api.strm as strm_api
from watch_assistant.api.strm import _cancel_operation, cancel_strm_operation
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import StrmOperation
from watch_assistant.security import AuthContext
from watch_assistant.services.strm_manifest import StrmManifestError
from watch_assistant.services.strm_operations import (
    StrmOperationError,
    StrmOperationKind,
    StrmOperationService,
    StrmOperationSummary,
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
        lease_owner = await service.get_lease_token(queued.operation_id)
        assert lease_owner is not None
        assert not hasattr(running, "lease_owner")
        assert lease_owner not in repr(running)
        completed = await service.complete(
            queued.operation_id,
            generated=2,
            unchanged=1,
            skipped=0,
            failed=0,
            retired=0,
            lease_owner=lease_owner,
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
async def test_terminal_commit_ack_loss_observes_durable_state(
    tmp_path: Path, monkeypatch
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
        )
        running = await service.start(operation.operation_id)
        lease_owner = await service.get_lease_token(operation.operation_id)
        assert running.status == "running"
        assert lease_owner is not None

        original_commit = AsyncSession.commit

        async def commit_then_lose_ack(session):
            await original_commit(session)
            raise RuntimeError("commit_acknowledgement_lost")

        monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_ack)
        completed = await service.complete(
            operation.operation_id,
            generated=1,
            unchanged=0,
            skipped=0,
            failed=0,
            retired=0,
            lease_owner=lease_owner,
        )

        assert completed.status == "succeeded"
        assert completed.generated == 1
        assert (await service.get(operation.operation_id)).status == "succeeded"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_terminal_fence_rejection_keeps_operation_running(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.INCREMENTAL,
        )
        running = await service.start(operation.operation_id)
        lease_owner = await service.get_lease_token(operation.operation_id)
        assert running.status == "running"
        assert lease_owner is not None

        async def durable_fence(_session: AsyncSession) -> bool:
            return False

        with pytest.raises(StrmOperationError, match="strm_operation_lease_lost"):
            await service.complete(
                operation.operation_id,
                generated=1,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=0,
                lease_owner=lease_owner,
                durable_fence=durable_fence,
            )

        persisted = await service.get(operation.operation_id)
        assert persisted.status == "running"
        assert persisted.generated == 0
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_applied_reconciliation_is_terminal_and_compare_and_set(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.CLEANUP,
        )
        await service.start(operation.operation_id)
        lease_owner = await service.get_lease_token(operation.operation_id)
        assert lease_owner is not None
        await service.fail(
            operation.operation_id,
            error_code="strm_operation_failed",
            lease_owner=lease_owner,
        )

        recovered = await service.reconcile_cleanup_applied(
            operation.operation_id, retired=2
        )
        repeated = await service.reconcile_cleanup_applied(
            operation.operation_id, retired=99
        )

        assert recovered.status == "succeeded"
        assert recovered.retired == 2
        assert recovered.error_code is None
        assert repeated.retired == 2

        running_operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-two",
            kind=StrmOperationKind.CLEANUP,
        )
        await service.start(running_operation.operation_id)
        with pytest.raises(StrmOperationError, match="strm_operation_not_reconcilable"):
            await service.reconcile_cleanup_applied(
                running_operation.operation_id, retired=1
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_api_cancel_syncs_linked_workflow_stage(monkeypatch):
    now = datetime.now(UTC)

    def summary(status: str, *, error_code: str | None = None):
        return StrmOperationSummary(
            operation_id="strm_op_cancel",
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind="full",
            status=status,
            workflow_id="workflow-one",
            generated=0,
            unchanged=0,
            skipped=0,
            failed=0,
            retired=0,
            error_code=error_code,
            created_at=now,
            started_at=now,
            finished_at=now if status == "cancelled" else None,
        )

    class Operations:
        async def get(self, _operation_id):
            return summary("running")

        async def cancel(self, _operation_id):
            return summary("cancelled", error_code="strm_operation_cancelled")

    calls = []

    async def sync_stage(_request, workflow_id, **kwargs):
        calls.append((workflow_id, kwargs))

    monkeypatch.setattr(strm_api, "_operation_service", lambda _request: Operations())
    monkeypatch.setattr(strm_api, "_sync_workflow_stage", sync_stage)
    request = SimpleNamespace(app=SimpleNamespace())
    context = AuthContext(identity="web", via_bearer=False)

    response = await cancel_strm_operation("strm_op_cancel", request, context)

    assert response.status == "cancelled"
    assert calls == [
        (
            "workflow-one",
            {
                "operation_id": "strm_op_cancel",
                "status": strm_api.WorkflowStageStatus.FAILED,
                "reason": "strm_cancelled",
                "error_code": "strm_operation_cancelled",
            },
        )
    ]


@pytest.mark.asyncio
async def test_api_finish_does_not_report_cancelled_operation_as_success(monkeypatch):
    now = datetime.now(UTC)
    operation = StrmOperationSummary(
        operation_id="strm_op_finish",
        library_id="library-one",
        source_scan_run_id="scan-one",
        kind="full",
        status="cancelled",
        workflow_id="workflow-one",
        generated=0,
        unchanged=0,
        skipped=0,
        failed=0,
        retired=0,
        error_code="strm_operation_cancelled",
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    class Operations:
        async def complete(self, *_args, **_kwargs):
            return operation

    calls = []

    async def sync_stage(_request, workflow_id, **kwargs):
        calls.append((workflow_id, kwargs))

    monkeypatch.setattr(strm_api, "_sync_workflow_stage", sync_stage)
    request = SimpleNamespace(app=SimpleNamespace())

    with pytest.raises(StrmManifestError, match="strm_operation_cancelled"):
        await strm_api._finish_operation(
            request,
            Operations(),
            operation.operation_id,
            workflow_id=operation.workflow_id,
            kind=StrmOperationKind.FULL,
            summary=SimpleNamespace(
                generated=1,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=0,
            ),
            lease_owner="lease-one",
        )

    assert calls == [
        (
            "workflow-one",
            {
                "operation_id": "strm_op_finish",
                "status": strm_api.WorkflowStageStatus.FAILED,
                "reason": "strm_finished",
                "error_code": "strm_operation_cancelled",
            },
        )
    ]


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
            lease_duration=timedelta(minutes=30),
        )
        assert running.status == "running"

        heartbeat = await service.heartbeat(
            operation.operation_id,
            lease_owner=await service.get_lease_token(operation.operation_id),
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
async def test_startup_recovery_preserves_a_live_operation_lease(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-live",
            kind=StrmOperationKind.INCREMENTAL,
        )
        running = await service.start(
            operation.operation_id,
            now=now,
            lease_duration=timedelta(minutes=5),
        )
        assert running.status == "running"

        recovered = await service.recover_incomplete(
            now=now + timedelta(minutes=1),
        )

        assert recovered == 0
        current = await service.get(operation.operation_id)
        assert current.status == "running"
        assert await service.is_lease_active(
            operation.operation_id,
            lease_owner=await service.get_lease_token(operation.operation_id),
            now=now + timedelta(minutes=1),
        )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_expired_recovery_fences_old_executor_before_new_claim(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
        old = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-old",
            kind=StrmOperationKind.INCREMENTAL,
        )
        await service.start(
            old.operation_id,
            now=now,
            lease_duration=timedelta(minutes=1),
        )
        old_owner = await service.get_lease_token(old.operation_id)
        assert old_owner is not None
        assert await service.recover_incomplete(now=now + timedelta(seconds=30)) == 0

        assert await service.recover_stale(
            max_age=timedelta(minutes=30),
            now=now + timedelta(minutes=2),
        ) == 1
        assert not await service.is_lease_active(
            old.operation_id,
            lease_owner=old_owner,
            now=now + timedelta(minutes=2),
        )

        new = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-new",
            kind=StrmOperationKind.INCREMENTAL,
        )
        claimed, acquired = await service.claim_start(
            new.operation_id,
            now=now + timedelta(minutes=2),
        )
        assert acquired is True
        assert claimed.status == "running"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_strm_operation_idempotency_reuses_only_matching_request(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        first = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
            workflow_id="workflow-one",
            idempotency_key="same-request",
        )
        repeated = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
            workflow_id="workflow-one",
            idempotency_key="same-request",
        )
        assert repeated.operation_id == first.operation_id
        with pytest.raises(StrmOperationError, match="idempotency_key_conflict"):
            await service.create(
                library_id="library-one",
                source_scan_run_id="scan-two",
                kind=StrmOperationKind.FULL,
                workflow_id="workflow-one",
                idempotency_key="same-request",
            )
        with pytest.raises(StrmOperationError, match="idempotency_key_conflict"):
            await service.create(
                library_id="library-one",
                source_scan_run_id="scan-one",
                kind=StrmOperationKind.FULL,
                workflow_id="workflow-two",
                idempotency_key="same-request",
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_same_library_claim_is_mutually_exclusive(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        first = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
            idempotency_key="first",
        )
        second = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-two",
            kind=StrmOperationKind.INCREMENTAL,
            idempotency_key="second",
        )
        claimed, acquired = await service.claim_start(first.operation_id)
        blocked, blocked_acquired = await service.claim_start(second.operation_id)
        assert claimed.status == "running"
        assert acquired is True
        assert blocked.status == "queued"
        assert blocked_acquired is False
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_same_library_claim_uses_database_condition_across_connections(
    tmp_path: Path,
):
    database_one = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    database_two = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database_one.engine)
    try:
        service_one = StrmOperationService(database_one.session_factory)
        service_two = StrmOperationService(database_two.session_factory)
        first = await service_one.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
            idempotency_key="first",
        )
        second = await service_one.create(
            library_id="library-one",
            source_scan_run_id="scan-two",
            kind=StrmOperationKind.INCREMENTAL,
            idempotency_key="second",
        )
        claims = await asyncio.gather(
            service_one.claim_start(first.operation_id),
            service_two.claim_start(second.operation_id),
        )
        assert sum(acquired for _, acquired in claims) == 1
        assert {summary.status for summary, _ in claims} == {"running", "queued"}
    finally:
        await database_one.engine.dispose()
        await database_two.engine.dispose()


@pytest.mark.asyncio
async def test_old_lease_cannot_update_progress_or_terminal_state(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    try:
        service = StrmOperationService(database.session_factory)
        operation = await service.create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
        )
        await service.start(operation.operation_id)
        old_lease = await service.get_lease_token(operation.operation_id)
        assert old_lease is not None
        async with database.session_factory() as session:
            row = await session.get(StrmOperation, operation.operation_id)
            assert row is not None
            row.lease_owner = "new-fenced-owner"
            row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()

        with pytest.raises(StrmOperationError, match="strm_operation_lease_lost"):
            await service.progress(
                operation.operation_id,
                generated=1,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=0,
                lease_owner=old_lease,
            )
        with pytest.raises(StrmOperationError, match="strm_operation_lease_lost"):
            await service.complete(
                operation.operation_id,
                generated=1,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=0,
                lease_owner=old_lease,
            )
        current = await service.get(operation.operation_id)
        assert current.status == "running"
        assert current.generated == 0
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
