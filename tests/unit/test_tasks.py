import pytest

from tests.unit.factories import make_task
from watch_assistant.models import TaskState
from watch_assistant.schemas import RemoteStatus
from watch_assistant.services.tasks import (
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
