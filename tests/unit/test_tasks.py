from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.factories import make_task
from watch_assistant.models import Resource, TaskState
from watch_assistant.schemas import RemoteObservation, RemoteStatus
from watch_assistant.services.tasks import (
    InvalidCancelState,
    InvalidRetryState,
    choose_existing_task,
    prepare_manual_retry,
    recover_after_restart,
    task_state_from_remote_observation,
    task_state_from_remote_status,
)


def test_submitting_without_remote_confirmation_becomes_uncertain():
    task = make_task(state=TaskState.SUBMITTING)
    task.lease_owner = "worker-1"
    task.lease_token = "old-token"

    recover_after_restart(task, remote_status=None)

    assert task.state == TaskState.UNCERTAIN
    assert task.lease_owner is None
    assert task.lease_token is None
    assert task.lease_expires_at is None


def test_recovery_uses_confirmed_remote_status():
    task = make_task(state=TaskState.SUBMITTING)

    recover_after_restart(task, RemoteStatus.ACCEPTED)

    assert task.state == TaskState.SUBMITTED


def test_remote_acceptance_never_means_available():
    assert task_state_from_remote_status(RemoteStatus.ACCEPTED) is TaskState.SUBMITTED
    assert task_state_from_remote_status(RemoteStatus.DOWNLOADING) is TaskState.DOWNLOADING
    assert task_state_from_remote_status(RemoteStatus.AVAILABLE) is TaskState.UNCERTAIN
    assert (
        task_state_from_remote_status(RemoteStatus.AVAILABLE, allow_available=True)
        is TaskState.UNCERTAIN
    )


@pytest.mark.parametrize(
    ("file_id", "parent_id", "is_directory"),
    [
        (None, "7", False),
        ("101", None, False),
        ("101", "7", True),
    ],
)
def test_incomplete_available_observation_stays_uncertain(
    file_id, parent_id, is_directory
):
    observation = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id=file_id,
        parent_id=parent_id,
        is_directory=is_directory,
    )

    assert task_state_from_remote_observation(observation) is TaskState.UNCERTAIN


def test_complete_file_observation_is_the_only_available_transition():
    observation = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )

    assert task_state_from_remote_observation(observation) is TaskState.AVAILABLE


def test_remote_observation_repr_redacts_identity_fields():
    observation = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )

    rendered = repr(observation)

    assert "101" not in rendered
    assert "7" not in rendered
    assert "availability_verified=True" in rendered


def test_recovery_requires_complete_file_observation_for_available():
    task = make_task(state=TaskState.SUBMITTING)

    recover_after_restart(task, RemoteStatus.AVAILABLE)
    assert task.state is TaskState.UNCERTAIN

    recover_after_restart(
        task,
        RemoteObservation(
            status=RemoteStatus.AVAILABLE,
            file_id="101",
            parent_id="7",
            is_directory=False,
        ),
    )
    assert task.state is TaskState.AVAILABLE


def test_restart_recovery_does_not_clear_a_live_lease():
    task = make_task(state=TaskState.SUBMITTING)
    task.lease_owner = "live-worker"
    task.lease_token = "live-token"
    task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)

    recover_after_restart(task, RemoteStatus.ACCEPTED)

    assert task.state is TaskState.SUBMITTING
    assert task.lease_owner == "live-worker"
    assert task.lease_token == "live-token"
    assert task.lease_expires_at is not None


def test_duplicate_resource_reuses_recent_task():
    existing = make_task(state=TaskState.SUBMITTED, age_hours=2)

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
            [make_task(state=TaskState.SUBMITTED, age_hours=25)],
            resource_id=existing.resource_id,
        )
        is None
    )


def test_manual_retry_is_explicit_and_clears_remote_outcome():
    task = make_task(state=TaskState.UNCERTAIN)
    task.remote_ref = "remote-123"
    task.error_code = "timeout"

    with pytest.raises(InvalidRetryState, match="uncertain_requires_verification"):
        prepare_manual_retry(task)

    assert task.state == TaskState.UNCERTAIN
    assert task.remote_ref == "remote-123"
    assert task.error_code == "timeout"

    with pytest.raises(InvalidRetryState):
        prepare_manual_retry(make_task(state=TaskState.SUBMITTED))


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
