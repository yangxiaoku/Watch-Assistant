import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from watch_assistant.models import TaskAction
from watch_assistant.services.tasks import TaskLease
from watch_assistant.worker import TaskWorker


class _LeaseService:
    def __init__(self):
        self.renew_calls = 0

    async def is_lease_active(self, _lease):
        return True

    async def renew(self, _lease, *, lease_duration):
        self.renew_calls += 1
        return False


def _lease() -> TaskLease:
    return TaskLease(
        task_id="task-fixture",
        lease_owner="worker-fixture",
        lease_token="token-fixture",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
        resource_id="resource-fixture",
        workflow_id=None,
        target_directory_id=None,
        action=TaskAction.OFFLINE_DOWNLOAD,
        encrypted_url_snapshot="encrypted-fixture",
        encrypted_password_snapshot=None,
        remote_ref=None,
    )


@pytest.mark.asyncio
async def test_external_call_cleans_heartbeat_when_operation_setup_fails():
    worker = object.__new__(TaskWorker)
    worker._lease_seconds = 0.03
    worker._tasks = _LeaseService()

    def operation_setup_failure():
        raise RuntimeError("operation setup failed")

    with pytest.raises(RuntimeError, match="operation setup failed"):
        await worker._run_external_call(_lease(), operation_setup_failure)

    await asyncio.sleep(0.05)
    assert worker._tasks.renew_calls == 0
