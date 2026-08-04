import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from watch_assistant.models import TaskAction
from watch_assistant.services.tasks import TaskLease
from watch_assistant.worker import TaskWorker, _LeaseClaimLost


class _LeaseService:
    def __init__(self):
        self.renew_calls = 0

    async def is_lease_active(self, _lease):
        return True

    async def renew(self, _lease, *, lease_duration):
        self.renew_calls += 1
        return False


class _InFlightRenewalService:
    def __init__(self):
        self.renew_started = asyncio.Event()
        self.renew_release = asyncio.Event()
        self.renew_completed = False
        self.renew_cancelled = False

    async def is_lease_active(self, _lease):
        return True

    async def renew(self, _lease, *, lease_duration):
        del lease_duration
        self.renew_started.set()
        try:
            await self.renew_release.wait()
        except asyncio.CancelledError:
            self.renew_cancelled = True
            raise
        self.renew_completed = True
        return True


class _ShutdownRenewalFailureService:
    def __init__(self):
        self.active_checks = 0
        self.renew_started = asyncio.Event()
        self.renew_release = asyncio.Event()

    async def is_lease_active(self, _lease):
        self.active_checks += 1
        if self.active_checks == 2:
            self.renew_release.set()
        return True

    async def renew(self, _lease, *, lease_duration):
        del lease_duration
        self.renew_started.set()
        await self.renew_release.wait()
        return False


class _DelayedRenewalService:
    def __init__(self, delay: float):
        self.delay = delay
        self.renew_started = asyncio.Event()
        self.renew_completed = False
        self.renew_cancelled = False

    async def is_lease_active(self, _lease):
        return True

    async def renew(self, _lease, *, lease_duration):
        del lease_duration
        self.renew_started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.renew_cancelled = True
            raise
        self.renew_completed = True
        return True


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


@pytest.mark.asyncio
async def test_external_call_waits_for_inflight_renewal_before_stopping_heartbeat():
    worker = object.__new__(TaskWorker)
    worker._lease_seconds = 0.03
    renewal = _InFlightRenewalService()
    worker._tasks = renewal

    async def operation():
        await renewal.renew_started.wait()
        renewal.renew_release.set()
        return "result"

    assert await worker._run_external_call(_lease(), operation) == "result"
    assert renewal.renew_completed is True
    assert renewal.renew_cancelled is False


@pytest.mark.asyncio
async def test_external_call_discards_result_when_shutdown_renewal_fails():
    worker = object.__new__(TaskWorker)
    worker._lease_seconds = 0.03
    renewal = _ShutdownRenewalFailureService()
    worker._tasks = renewal

    async def operation():
        await renewal.renew_started.wait()
        return "result"

    with pytest.raises(_LeaseClaimLost):
        await worker._run_external_call(_lease(), operation)


@pytest.mark.asyncio
async def test_external_call_waits_for_slow_live_renewal_before_returning_result():
    worker = object.__new__(TaskWorker)
    worker._lease_seconds = 0.3
    renewal = _DelayedRenewalService(0.12)
    worker._tasks = renewal

    async def operation():
        await renewal.renew_started.wait()
        return "result"

    assert await worker._run_external_call(_lease(), operation) == "result"
    assert renewal.renew_completed is True
    assert renewal.renew_cancelled is False
