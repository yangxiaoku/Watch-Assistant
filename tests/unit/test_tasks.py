from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.factories import make_task
from watch_assistant.models import Resource, TaskState
from watch_assistant.schemas import RemoteStatus
from watch_assistant.services.tasks import (
    InvalidCancelState,
    InvalidRetryState,
    choose_existing_task,
    prepare_manual_retry,
    recover_after_restart,
)


def test_submitting_without_remote_confirmation_becomes_uncertain():
    task = make_task(state=TaskState.SUBMITTING)
    task.lease_owner = "worker-1"

    recover_after_restart(task, remote_status=None)

    assert task.state == TaskState.UNCERTAIN
    assert task.lease_owner is None
    assert task.lease_expires_at is None


def test_recovery_uses_confirmed_remote_status():
    task = make_task(state=TaskState.SUBMITTING)

    recover_after_restart(task, RemoteStatus.ACCEPTED)

    assert task.state == TaskState.ACCEPTED


def test_duplicate_resource_reuses_recent_task():
    existing = make_task(state=TaskState.ACCEPTED, age_hours=2)

    assert (
        choose_existing_task([existing], resource_id=existing.resource_id) is existing
    )
    assert (
        choose_existing_task(
            [make_task(state=TaskState.FAILED, age_hours=2)],
            resource_id=existing.resource_id,
        )
        is None
    )
    assert (
        choose_existing_task(
            [make_task(state=TaskState.ACCEPTED, age_hours=25)],
            resource_id=existing.resource_id,
        )
        is None
    )


def test_manual_retry_is_explicit_and_clears_remote_outcome():
    task = make_task(state=TaskState.UNCERTAIN)
    task.remote_ref = "remote-123"
    task.error_code = "timeout"

    prepare_manual_retry(task)

    assert task.state == TaskState.QUEUED
    assert task.remote_ref is None
    assert task.error_code is None

    with pytest.raises(InvalidRetryState):
        prepare_manual_retry(make_task(state=TaskState.ACCEPTED))


@pytest.mark.asyncio
async def test_task_service_cancels_only_queued_tasks(tmp_path):
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.services.tasks import TaskService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            resource = Resource(
                id="res_test",
                kind="magnet",
                canonical_key="magnet:test",
                encrypted_url="encrypted",
                name="Test",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            session.add(resource)
            await session.flush()
            queued = make_task(resource_id=resource.id)
            running = make_task(resource_id=resource.id, state=TaskState.SUBMITTING)
            session.add_all([queued, running])
            await session.commit()
        service = TaskService(database.session_factory)
        cancelled = await service.cancel(queued.id)
        assert cancelled.state == TaskState.CANCELLED
        assert cancelled.error_code == "cancelled"
        with pytest.raises(InvalidCancelState):
            await service.cancel(running.id)
    finally:
        await database.engine.dispose()
