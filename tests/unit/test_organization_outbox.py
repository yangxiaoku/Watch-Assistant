from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from test_organization_operations import _database, _item, _operation

from watch_assistant.models import (
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    OrganizationOperationStatus,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationConflict,
    OrganizationOperationLeaseUnavailable,
    OrganizationOperationService,
)
from watch_assistant.services.organization_outbox import (
    DIRTY_CONSUMED,
    DIRTY_PENDING,
    DirectoryDirtyOutboxService,
    OrganizationOutboxError,
)
from watch_assistant.services.organization_plan import OrganizationPlanService


async def _claimed(database):
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    lease = await service.claim(operation.operation_id, expected_revision=1)
    return service, operation, lease


@pytest.mark.asyncio
async def test_organized_completion_and_directory_dedup_are_atomic(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    completed = await service.complete_organized_with_dirty_events(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        source_directory_id="7000",
        target_directory_id="7000",
    )
    assert completed.status is OrganizationOperationStatus.ORGANIZED
    async with database.session_factory() as session:
        events = list((await session.scalars(select(DirectoryDirtyEvent))).all())
    assert len(events) == 1
    assert events[0].status == "pending"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_generation_coalesces_running_change_and_requeues_latest_generation(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    first_operation = await _operation(database, key="generation-operation-1")
    first_lease = await service.claim(first_operation.operation_id, expected_revision=1)
    await service.finish(
        first_operation.operation_id,
        expected_revision=first_lease.revision,
        lease_token=first_lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    outbox = DirectoryDirtyOutboxService()
    now = datetime.now(UTC) + timedelta(seconds=1)
    running = await outbox.claim_generation(database.session_factory, now=now)
    assert running is not None
    assert running.generation == 1

    second_plan = await OrganizationPlanService(database.session_factory).create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(_item(),),
        parser_version="parser-v2",
    )
    second_operation = await OrganizationOperationService(
        database.session_factory
    ).create(second_plan.plan_id, idempotency_key="generation-operation-2")
    second_lease = await service.claim(second_operation.operation_id, expected_revision=1)
    await service.finish(
        second_operation.operation_id,
        expected_revision=second_lease.revision,
        lease_token=second_lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    async with database.session_factory() as session:
        queue = await session.scalar(select(DirectoryDirtyGeneration))
        assert queue is not None
        assert queue.generation == 2
        assert queue.status == "dirty"

    assert await outbox.complete(database.session_factory, running, now=now)
    async with database.session_factory() as session:
        queue = await session.scalar(select(DirectoryDirtyGeneration))
        assert queue is not None
        assert queue.generation == 2
        assert queue.status == "queued"
        assert queue.lease_token is None

    latest = await outbox.claim_generation(database.session_factory, now=now)
    assert latest is not None
    assert latest.generation == 2
    assert latest.operation_id == second_operation.operation_id
    assert await outbox.complete(database.session_factory, latest, now=now)
    async with database.session_factory() as session:
        queue = await session.scalar(select(DirectoryDirtyGeneration))
        assert queue is not None
        assert queue.generation == 2
        assert queue.status == "clean"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_repeated_or_stale_completion_does_not_duplicate_events(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    with pytest.raises(OrganizationOperationLeaseUnavailable):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            source_directory_id="7000",
            target_directory_id="8000",
        )
    async with database.session_factory() as session:
        assert (
            len(list((await session.scalars(select(DirectoryDirtyEvent))).all())) == 2
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_terminal_non_success_states_never_enqueue_events(tmp_path: Path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    with pytest.raises(OrganizationOperationLeaseUnavailable):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=1,
            lease_token="a" * 32,
            source_directory_id="7000",
            target_directory_id="8000",
        )
    lease = await service.claim(operation.operation_id, expected_revision=1)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code="local_failure",
    )
    async with database.session_factory() as session:
        assert await session.scalar(select(DirectoryDirtyEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_invalid_scope_rolls_back_organized_state_and_event(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    with pytest.raises(OrganizationOperationConflict, match="invalid_directory_id"):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            source_directory_id="outside/path",
            target_directory_id="target-directory",
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.ORGANIZING
    assert current.revision == lease.revision
    async with database.session_factory() as session:
        assert await session.scalar(select(DirectoryDirtyEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_completion_rejects_directory_ids_outside_plan_scope(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    with pytest.raises(
        OrganizationOperationConflict, match="directory_scope_unverified"
    ):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            source_directory_id="7000",
            target_directory_id="8000",
            directory_ids=("7000", "8000", "9000"),
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.ORGANIZING
    assert current.revision == lease.revision
    async with database.session_factory() as session:
        assert await session.scalar(select(DirectoryDirtyEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_operation_and_event(tmp_path: Path):
    database = await _database(tmp_path)

    class _FailingOutbox:
        async def enqueue_directory_dirty(self, *_args, **_kwargs):
            raise RuntimeError("persistence detail must stay private")

    service = OrganizationOperationService(
        database.session_factory, outbox_service=_FailingOutbox()
    )
    operation = await _operation(database)
    lease = await service.claim(operation.operation_id, expected_revision=1)
    with pytest.raises(
        OrganizationOperationConflict, match="outbox_persistence_failed"
    ):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            source_directory_id="7000",
            target_directory_id="8000",
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.ORGANIZING
    async with database.session_factory() as session:
        assert await session.scalar(select(DirectoryDirtyEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_operation_has_no_dirty_event(tmp_path: Path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    cancelled = await service.cancel(operation.operation_id, expected_revision=1)
    assert cancelled.status is OrganizationOperationStatus.CANCELLED
    with pytest.raises(OrganizationOperationLeaseUnavailable):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=1,
            lease_token="a" * 32,
            source_directory_id="7000",
            target_directory_id="8000",
        )
    async with database.session_factory() as session:
        assert await session.scalar(select(DirectoryDirtyEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_outbox_migration_and_unique_key_are_forward_only(tmp_path: Path):
    database = await _database(tmp_path)
    async with database.engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync).get_columns("directory_dirty_events")
            }
        )
        unique_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("directory_dirty_events")
        )
    assert {"id", "operation_id", "directory_id", "event_kind", "status"} <= columns
    assert any(
        constraint["column_names"] == ["operation_id", "directory_id", "event_kind"]
        for constraint in unique_constraints
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_outbox_service_is_idempotent_and_rejects_sensitive_shape(tmp_path: Path):
    database = await _database(tmp_path)
    operation = await _operation(database)
    outbox = DirectoryDirtyOutboxService()
    async with database.session_factory() as session:
        assert (
            await outbox.enqueue_directory_dirty(
                session,
                operation_id=operation.operation_id,
                directory_ids=("directory-a", "directory-a"),
            )
            == 1
        )
        await session.commit()
    async with database.session_factory() as session:
        assert (
            await outbox.enqueue_directory_dirty(
                session,
                operation_id=operation.operation_id,
                directory_ids=("directory-a",),
            )
            == 0
        )
    with pytest.raises(OrganizationOutboxError, match="invalid_directory_id"):
        async with database.session_factory() as session:
            await outbox.enqueue_directory_dirty(
                session,
                operation_id=operation.operation_id,
                directory_ids=("cookie=secret",),
            )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_lease_reclaims_and_retries_with_backoff(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    outbox = DirectoryDirtyOutboxService()
    now = datetime.now(UTC) + timedelta(seconds=1)
    first = await outbox.claim_next(database.session_factory, now=now)
    assert first is not None
    assert first.attempts == 1
    async with database.session_factory() as session:
        row = await session.get(DirectoryDirtyEvent, first.event_id)
        assert row is not None
        row.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()
    reclaimed = await outbox.claim_next(database.session_factory, now=now)
    assert reclaimed is not None
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.attempts == 2
    assert await outbox.retry(
        database.session_factory,
        reclaimed,
        error_code="scan_incomplete",
        now=now,
    )
    async with database.session_factory() as session:
        row = await session.get(DirectoryDirtyEvent, reclaimed.event_id)
        assert row is not None
        assert row.status == DIRTY_PENDING
        assert row.available_at.replace(tzinfo=UTC) > now
        row.available_at = now
        await session.commit()
    third = await outbox.claim_next(database.session_factory, now=now)
    assert third is not None
    assert await outbox.complete(
        database.session_factory, third, status=DIRTY_CONSUMED, now=now
    )
    async with database.session_factory() as session:
        row = await session.get(DirectoryDirtyEvent, third.event_id)
        assert row is not None
        assert row.status == DIRTY_CONSUMED
        assert row.lease_token is None
        assert row.lease_expires_at is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_expired_generation_completion_cannot_release_old_lease(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    outbox = DirectoryDirtyOutboxService()
    now = datetime.now(UTC) + timedelta(seconds=1)
    claimed = await outbox.claim_generation(database.session_factory, now=now)
    assert claimed is not None

    async with database.session_factory() as session:
        queue = await session.get(DirectoryDirtyGeneration, claimed.queue_id)
        event = await session.get(DirectoryDirtyEvent, claimed.event_id)
        assert queue is not None
        assert event is not None
        queue.lease_expires_at = now - timedelta(seconds=1)
        event.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()

    assert not await outbox.complete(database.session_factory, claimed, now=now)
    async with database.session_factory() as session:
        queue = await session.get(DirectoryDirtyGeneration, claimed.queue_id)
        event = await session.get(DirectoryDirtyEvent, claimed.event_id)
        assert queue is not None
        assert event is not None
        assert queue.status == "running"
        assert queue.lease_token == claimed.lease_token
        assert event.status == "running"
        assert event.lease_token == claimed.lease_token
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_expired_event_completion_cannot_consume_old_lease(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    outbox = DirectoryDirtyOutboxService()
    now = datetime.now(UTC) + timedelta(seconds=1)
    claimed = await outbox.claim_next(database.session_factory, now=now)
    assert claimed is not None

    async with database.session_factory() as session:
        event = await session.get(DirectoryDirtyEvent, claimed.event_id)
        assert event is not None
        event.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()

    assert not await outbox.complete(database.session_factory, claimed, now=now)
    async with database.session_factory() as session:
        event = await session.get(DirectoryDirtyEvent, claimed.event_id)
        assert event is not None
        assert event.status == "running"
        assert event.lease_token == claimed.lease_token
    await database.engine.dispose()


def test_dirty_event_repr_redacts_identifiers():
    event = DirectoryDirtyEvent(
        id="evt-secret-id",
        operation_id="op-secret-id",
        directory_id="directory-secret-id",
        event_kind="directory_dirty",
        status="pending",
    )
    rendered = repr(event)
    assert "secret" not in rendered
