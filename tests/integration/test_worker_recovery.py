import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import (
    EvidenceSource,
    EvidenceStatus,
    RemoteObservation,
    RemoteStatus,
    SubmissionResult,
    WorkflowCreateRequest,
    WorkflowStageName,
    WorkflowStagePatch,
    WorkflowStageStatus,
)
from watch_assistant.services.inventory_push_guard import (
    InventoryPushCheck,
    InventoryRefreshEvidence,
)
from watch_assistant.services.library_inventory import InventoryDecision
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.workflows import (
    WorkflowConflict,
    WorkflowService,
    advance_availability_from_evidence,
    record_evidence,
    sync_child_stage,
)
from watch_assistant.worker import TaskWorker, _LeaseClaimLost


class FakeAdapter:
    def __init__(self):
        self.submissions = 0
        self.target_cids = []
        self.remote_status = None
        self.status_lookups = 0
        self.status_target_directory_ids = []

    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult:
        self.submissions += 1
        self.target_cids.append(target_cid)
        assert url.startswith("magnet:")
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-123")

    async def save_share(self, url: str, password: str | None) -> SubmissionResult:
        self.submissions += 1
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-share")

    async def get_status_for_task(
        self, remote_ref: str, *, target_directory_id: str | None
    ):
        self.status_target_directory_ids.append(target_directory_id)
        self.status_lookups += 1
        return self.remote_status


class SlowSubmissionAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult:
        self.submissions += 1
        self.target_cids.append(target_cid)
        assert url.startswith("magnet:")
        self.started.set()
        await self.release.wait()
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-slow")


class CancelledSubmissionAdapter(FakeAdapter):
    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult:
        self.submissions += 1
        self.target_cids.append(target_cid)
        raise asyncio.CancelledError


class UncertainReceiptAdapter(FakeAdapter):
    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult:
        self.submissions += 1
        self.target_cids.append(target_cid)
        assert url.startswith("magnet:")
        return SubmissionResult(
            status=RemoteStatus.UNCERTAIN,
            remote_ref="remote-recovered",
            error_code="adapter_uncertain",
        )


class EventRecorder:
    def __init__(self):
        self.events = []

    async def log_event(self, event, **kwargs):
        self.events.append((event, kwargs))


class RefreshingInventoryGuard:
    def __init__(
        self,
        after_refresh: InventoryPushCheck,
        initial_code: str = "inventory_index_stale",
    ):
        self.after_refresh = after_refresh
        self.initial_code = initial_code
        self.checks = 0

    async def check(self, _resource_id):
        self.checks += 1
        if self.checks == 1:
            return InventoryPushCheck(False, self.initial_code)
        return self.after_refresh


class AdvisoryInventoryGuard:
    def __init__(self, code: str, decision: InventoryDecision):
        self.code = code
        self.decision = decision

    async def check(self, _resource_id):
        return InventoryPushCheck(True, self.code, self.decision)


async def _database(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    await initialize_database(database.engine)
    return database


async def _add_resource(database, crypto):
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_magnet",
                kind="magnet",
                canonical_key="magnet:abcdef0123456789abcdef0123456789abcdef01",
                encrypted_url=crypto.encrypt(
                    "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
                ),
                name="Movie",
                source="PanSou",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await session.commit()


async def _add_share_resource(database, crypto):
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_share",
                kind="115_share",
                canonical_key="115_share:res_share",
                encrypted_url=crypto.encrypt("https://115.com/s/share"),
                encrypted_password=crypto.encrypt("1234"),
                name="Share",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await session.commit()


@pytest.mark.integration
async def test_task_creation_is_idempotent_and_worker_accepts_submission(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)

    first, reused = await service.create("res_magnet")
    duplicate, duplicate_reused = await service.create("res_magnet")

    assert reused is False
    assert duplicate_reused is True
    assert duplicate.id == first.id

    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="test-worker")
    processed = await worker.run_once()
    task = await service.get(first.id)

    assert processed is True
    assert task.state == TaskState.SUBMITTED
    assert task.remote_ref == "remote-123"
    assert adapter.submissions == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_sqlite_claim_has_one_winner_when_workers_read_same_candidate(tmp_path):
    database = await _database(tmp_path)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}"
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    await database.engine.dispose()

    candidate_selects = 0
    counter_lock = threading.Lock()
    select_barrier = threading.Barrier(2)

    def synchronize_candidate_reads(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal candidate_selects
        if (
            "FROM tasks" in statement
            and "ORDER BY tasks.created_at" in statement
            and "LIMIT" in statement
        ):
            with counter_lock:
                candidate_selects += 1
                should_wait = candidate_selects <= 2
            if should_wait:
                select_barrier.wait(timeout=5)

    async def claim_on_independent_engine(owner: str):
        peer_database = create_database(database_url)
        event.listen(
            peer_database.engine.sync_engine,
            "before_cursor_execute",
            synchronize_candidate_reads,
        )
        try:
            worker = TaskWorker(
                peer_database.session_factory,
                crypto,
                FakeAdapter(),
                owner=owner,
            )
            return await worker._claim_one()
        finally:
            event.remove(
                peer_database.engine.sync_engine,
                "before_cursor_execute",
                synchronize_candidate_reads,
            )
            await peer_database.engine.dispose()

    leases = await asyncio.gather(
        asyncio.to_thread(lambda: asyncio.run(claim_on_independent_engine("worker-a"))),
        asyncio.to_thread(lambda: asyncio.run(claim_on_independent_engine("worker-b"))),
    )

    assert candidate_selects >= 2
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    check_database = create_database(database_url)
    service = TaskService(check_database.session_factory)
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.attempts == 1
    assert stored.lease_token is not None
    await check_database.engine.dispose()


@pytest.mark.integration
async def test_worker_renews_lease_during_slow_external_submission(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    adapter = SlowSubmissionAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="slow-worker",
        lease_seconds=0.3,
    )

    run_task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    await asyncio.sleep(0.7)
    in_flight = await service.get(task.id)
    assert in_flight is not None
    assert in_flight.state is TaskState.SUBMITTING
    assert in_flight.lease_owner == "slow-worker"
    assert in_flight.lease_token is not None

    adapter.release.set()
    assert await asyncio.wait_for(run_task, timeout=2) is True
    finished = await service.get(task.id)
    assert finished is not None
    assert finished.state is TaskState.SUBMITTED
    assert finished.remote_ref == "remote-slow"
    assert finished.lease_owner is None
    assert finished.lease_token is None
    await database.engine.dispose()


@pytest.mark.integration
async def test_renewal_rejects_database_delay_after_lease_expiry(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    lease = await service.claim_next(
        owner="slow-renew-worker",
        lease_duration=timedelta(milliseconds=40),
    )
    assert lease is not None

    def delay_renewal_update(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if "UPDATE tasks" in statement and "lease_expires_at" in statement:
            time.sleep(0.08)

    event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        delay_renewal_update,
    )
    try:
        renewed = await service.renew(
            lease,
            lease_duration=timedelta(seconds=1),
        )
    finally:
        event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            delay_renewal_update,
        )

    assert renewed is False
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.lease_token == lease.lease_token
    assert stored.lease_expires_at is not None
    stored_expiry = stored.lease_expires_at.replace(tzinfo=UTC)
    assert stored_expiry <= datetime.now(UTC)
    await database.engine.dispose()


@pytest.mark.integration
async def test_liveness_check_rejects_slow_query_after_lease_expiry(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    lease = await service.claim_next(
        owner="slow-liveness-worker",
        lease_duration=timedelta(milliseconds=40),
    )
    assert lease is not None

    def delay_liveness_query(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if "SELECT tasks.lease_expires_at" in statement:
            time.sleep(0.08)

    event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        delay_liveness_query,
    )
    try:
        active = await service.is_lease_active(lease)
    finally:
        event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            delay_liveness_query,
        )

    assert active is False
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.lease_token == lease.lease_token
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("renewal_failure", ["false", "timeout"])
async def test_renewal_failure_stops_follow_up_submission_and_marks_uncertain(
    tmp_path, renewal_failure
):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    adapter = SlowSubmissionAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="renewal-failure-worker",
        lease_seconds=0.3,
    )
    if renewal_failure == "false":
        worker._tasks.renew = AsyncMock(return_value=False)
    else:
        renewal_started = asyncio.Event()

        async def blocked_renew(*_args, **_kwargs):
            renewal_started.set()
            await asyncio.Event().wait()

        worker._tasks.renew = blocked_renew

    run_task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    if renewal_failure == "timeout":
        await asyncio.wait_for(renewal_started.wait(), timeout=2)

    assert await asyncio.wait_for(run_task, timeout=2) is True
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.UNCERTAIN
    assert stored.error_code == "lease_claim_lost"
    assert stored.error_message == "任务执行权已变化，外部结果待确认，未继续提交。"
    assert stored.lease_owner is None
    assert stored.lease_token is None
    assert stored.lease_expires_at is None

    # A fenced loss is terminal for this attempt; the worker cannot submit again.
    assert await worker.run_once() is False
    assert adapter.submissions == 1
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("iteration", range(20))
async def test_external_call_rechecks_owner_before_adapter_invocation(
    tmp_path, iteration
):
    del iteration
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    old_lease = await service.claim_next(
        owner="old-owner",
        lease_duration=timedelta(minutes=1),
    )
    assert old_lease is not None

    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="old-owner",
        lease_seconds=60,
    )
    original_is_active = worker._tasks.is_lease_active
    checks = 0
    new_lease = None

    async def takeover_after_initial_check(lease):
        nonlocal checks, new_lease
        checks += 1
        if checks == 2:
            async with database.session_factory() as session:
                stored = await session.get(Task, task.id)
                assert stored is not None
                stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            new_lease = await service.claim_expired(
                owner="new-owner",
                lease_duration=timedelta(minutes=1),
            )
            assert new_lease is not None
        return await original_is_active(lease)

    worker._tasks.is_lease_active = takeover_after_initial_check

    with pytest.raises(_LeaseClaimLost):
        await worker._run_external_call(
            old_lease,
            lambda: adapter.submit_magnet("magnet:?xt=urn:btih:fixture"),
        )

    assert checks >= 2
    assert new_lease is not None
    assert adapter.submissions == 0
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.lease_owner == "new-owner"
    assert stored.lease_token == new_lease.lease_token
    await database.engine.dispose()


@pytest.mark.integration
async def test_cancelled_external_submission_is_fenced_and_recoverable(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    adapter = CancelledSubmissionAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="cancelled-worker",
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()

    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.UNCERTAIN
    assert stored.error_code == "lease_claim_lost"
    assert stored.lease_owner is None
    assert stored.lease_token is None
    assert stored.lease_expires_at is None
    assert await worker.run_once() is False
    assert adapter.submissions == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_finish_database_failure_marks_claim_uncertain_without_retry(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="db-failure-worker",
    )

    async def fail_finish(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    worker._tasks.finish_submission = fail_finish

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.UNCERTAIN
    assert stored.error_code == "lease_claim_lost"
    assert stored.lease_owner is None
    assert stored.lease_token is None
    assert adapter.submissions == 1
    assert await worker.run_once() is False
    await database.engine.dispose()


@pytest.mark.integration
async def test_recover_expired_does_not_take_over_a_live_lease(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    claim_time = datetime.now(UTC)
    lease = await service.claim_next(
        owner="live-worker",
        lease_duration=timedelta(seconds=60),
        now=claim_time,
    )
    assert lease is not None

    worker = TaskWorker(
        database.session_factory,
        crypto,
        FakeAdapter(),
        owner="recovery-worker",
        lease_seconds=60,
    )
    assert await worker.recover_expired() == 0
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.lease_owner == "live-worker"
    assert stored.lease_token == lease.lease_token
    await database.engine.dispose()


@pytest.mark.integration
async def test_stale_worker_cannot_renew_or_finalize_after_expired_claim_takeover(
    tmp_path,
):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    claim_time = datetime.now(UTC)
    old_lease = await service.claim_next(
        owner="old-worker",
        lease_duration=timedelta(seconds=30),
        now=claim_time,
    )
    assert old_lease is not None

    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        assert stored is not None
        stored.lease_expires_at = claim_time - timedelta(seconds=1)
        await session.commit()

    new_lease = await service.claim_expired(
        owner="new-worker",
        lease_duration=timedelta(seconds=30),
        now=claim_time,
    )
    assert new_lease is not None
    assert new_lease.lease_token != old_lease.lease_token
    assert (
        await service.renew(
            old_lease,
            lease_duration=timedelta(seconds=30),
            now=claim_time,
        )
        is False
    )
    assert await service.mark_lease_lost(old_lease) is None
    current = await service.get(task.id)
    assert current is not None
    assert current.state is TaskState.SUBMITTING
    assert current.lease_owner == "new-worker"
    assert current.lease_token == new_lease.lease_token

    accepted = await service.finish_submission(
        new_lease,
        SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-new"),
    )
    stale = await service.finish_submission(
        old_lease,
        SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-old"),
    )

    assert accepted is not None
    assert stale is None
    finished = await service.get(task.id)
    assert finished is not None
    assert finished.state is TaskState.SUBMITTED
    assert finished.remote_ref == "remote-new"
    assert finished.lease_owner is None
    assert finished.lease_token is None
    await database.engine.dispose()


@pytest.mark.integration
async def test_submission_terminal_write_is_fenced_again_if_local_finalize_is_slow(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (WorkflowStageName.INSPECTION, WorkflowStageName.APPROVAL):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await service.create("res_magnet", workflow_id=workflow.id)
    lease = await service.claim_next(
        owner="slow-finalizer",
        lease_duration=timedelta(milliseconds=30),
    )
    assert lease is not None

    async def slow_record(*args, **kwargs):
        evidence = await record_evidence(*args, **kwargs)
        await asyncio.sleep(0.08)
        return evidence

    monkeypatch.setattr(
        "watch_assistant.services.tasks.record_evidence", slow_record
    )

    finished = await service.finish_submission(
        lease,
        SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-slow"),
    )

    assert finished is None
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.remote_ref is None
    assert stored.lease_owner == "slow-finalizer"
    assert stored.lease_token == lease.lease_token
    assert await service.evidence(task.id) == []
    workflow_state = await workflow_service.get(workflow.id)
    push = next(
        stage for stage in workflow_state.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert push.status is WorkflowStageStatus.RUNNING
    await database.engine.dispose()


@pytest.mark.integration
async def test_recovery_terminal_write_is_fenced_again_if_local_finalize_is_slow(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    lease = await service.claim_next(
        owner="slow-recovery-finalizer",
        lease_duration=timedelta(milliseconds=30),
    )
    assert lease is not None

    async def slow_apply(session, claimed_task, remote_status, **kwargs):
        del session, remote_status, kwargs
        claimed_task.state = TaskState.SUBMITTED
        await asyncio.sleep(0.08)

    monkeypatch.setattr(
        "watch_assistant.services.tasks.apply_remote_status", slow_apply
    )

    finished = await service.finish_recovery(
        lease,
        RemoteStatus.ACCEPTED,
    )

    assert finished is None
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.remote_ref is None
    assert stored.lease_owner == "slow-recovery-finalizer"
    assert stored.lease_token == lease.lease_token
    await database.engine.dispose()


@pytest.mark.integration
async def test_submission_terminal_fence_rejects_database_delay_after_expiry(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    lease = await service.claim_next(
        owner="slow-terminal-fence",
        lease_duration=timedelta(milliseconds=40),
    )
    assert lease is not None
    fence_updates = 0

    def delay_terminal_fence(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal fence_updates
        normalized = statement.upper()
        if (
            "UPDATE TASKS SET UPDATED_AT" in normalized
            and "TASKS.LEASE_OWNER" in normalized
        ):
            fence_updates += 1
            if fence_updates == 2:
                time.sleep(0.08)

    event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        delay_terminal_fence,
    )
    try:
        finished = await service.finish_submission(
            lease,
            SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref="remote-slow"),
        )
    finally:
        event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            delay_terminal_fence,
        )

    assert finished is None
    assert fence_updates == 2
    stored = await service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.SUBMITTING
    assert stored.remote_ref is None
    assert stored.lease_token == lease.lease_token
    await database.engine.dispose()


@pytest.mark.integration
async def test_reusing_available_task_advances_new_workflow_availability(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)

    existing, _ = await task_service.create("res_magnet")
    async with database.session_factory() as session:
        stored = await session.get(Task, existing.id)
        assert stored is not None
        stored.remote_ref = "remote-existing"
        await session.commit()

    adapter = FakeAdapter()
    adapter.remote_status = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )
    await task_service.reconcile(existing.id, adapter)

    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )

    reused, reused_flag = await task_service.create(
        "res_magnet", workflow_id=workflow.id
    )

    assert reused_flag is True
    assert reused.id == existing.id
    updated = await workflow_service.get(workflow.id)
    stages = {stage.stage.value: stage.status.value for stage in updated.stages}
    assert stages["push"] == "succeeded"
    assert stages["availability"] == "succeeded"
    assert all(
        evidence.workflow_id == workflow.id
        for evidence in await task_service.evidence(existing.id)
    )
    await database.engine.dispose()


@pytest.mark.integration
async def test_reusing_available_task_requires_owned_verified_evidence(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)

    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, reused = await task_service.create(
        "res_magnet", workflow_id=workflow.id
    )
    assert reused is False

    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        assert stored is not None
        stored.state = TaskState.AVAILABLE
        await session.commit()

    with pytest.raises(WorkflowConflict, match="workflow_evidence_required"):
        await task_service.create("res_magnet", workflow_id=workflow.id)

    stored = await task_service.get(task.id)
    assert stored is not None
    assert stored.state is TaskState.AVAILABLE
    assert stored.workflow_id == workflow.id
    unchanged = await workflow_service.get(workflow.id)
    push = next(
        stage for stage in unchanged.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert push.status is WorkflowStageStatus.RUNNING
    assert push.child_id == task.id
    await database.engine.dispose()


@pytest.mark.integration
async def test_concurrent_terminal_stage_replay_preserves_first_reason(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )

    update_barrier = threading.Barrier(2)
    update_count = 0
    update_lock = threading.Lock()

    def synchronize_stage_updates(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal update_count
        normalized = statement.upper()
        if (
            "UPDATE WORKFLOW_STAGES" not in normalized
            or "UPDATED_AT" not in normalized
        ):
            return
        with update_lock:
            update_count += 1
            should_wait = update_count <= 2
        if should_wait:
            update_barrier.wait(timeout=5)

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}"

    def patch_on_peer(reason: str, error_code: str):
        async def run():
            peer_database = create_database(database_url)
            event.listen(
                peer_database.engine.sync_engine,
                "before_cursor_execute",
                synchronize_stage_updates,
            )
            try:
                return await WorkflowService(
                    peer_database.session_factory
                ).patch_stage(
                    workflow.id,
                    WorkflowStageName.PUSH,
                    WorkflowStagePatch(
                        status=WorkflowStageStatus.FAILED,
                        reason=reason,
                        error_code=error_code,
                    ),
                )
            finally:
                event.remove(
                    peer_database.engine.sync_engine,
                    "before_cursor_execute",
                    synchronize_stage_updates,
                )
                await peer_database.engine.dispose()

        return asyncio.run(run())

    first, second = await asyncio.gather(
        asyncio.to_thread(patch_on_peer, "终态原因 A", "failure_a"),
        asyncio.to_thread(patch_on_peer, "终态原因 B", "failure_b"),
    )

    assert update_count >= 2
    response_reasons = {
        next(stage for stage in response.stages if stage.stage is WorkflowStageName.PUSH).reason
        for response in (first, second)
    }
    persisted = await workflow_service.get(workflow.id)
    persisted_push = next(
        stage for stage in persisted.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert response_reasons == {persisted_push.reason}
    assert {response.status for response in (first, second)} == {persisted.status}
    assert persisted_push.reason in {"终态原因 A", "终态原因 B"}
    assert persisted_push.error_code in {"failure_a", "failure_b"}
    await database.engine.dispose()


@pytest.mark.integration
async def test_concurrent_child_terminal_updates_preserve_first_reason(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, reused = await task_service.create(
        "res_magnet", workflow_id=workflow.id
    )
    assert reused is False

    update_barrier = threading.Barrier(2)
    update_count = 0
    update_lock = threading.Lock()

    def synchronize_stage_updates(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal update_count
        normalized = statement.upper()
        if (
            "UPDATE WORKFLOW_STAGES" not in normalized
            or "UPDATED_AT" not in normalized
        ):
            return
        with update_lock:
            update_count += 1
            should_wait = update_count <= 2
        if should_wait:
            update_barrier.wait(timeout=5)

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}"

    def sync_on_peer(reason: str, error_code: str):
        async def run():
            peer_database = create_database(database_url)
            event.listen(
                peer_database.engine.sync_engine,
                "before_cursor_execute",
                synchronize_stage_updates,
            )
            try:
                async with peer_database.session_factory() as session:
                    workflow_result = await sync_child_stage(
                        session,
                        workflow.id,
                        WorkflowStageName.PUSH,
                        child_type="task",
                        child_id=task.id,
                        status=WorkflowStageStatus.FAILED,
                        reason=reason,
                        error_code=error_code,
                    )
                    await session.commit()
                    return workflow_result
            finally:
                event.remove(
                    peer_database.engine.sync_engine,
                    "before_cursor_execute",
                    synchronize_stage_updates,
                )
                await peer_database.engine.dispose()

        return asyncio.run(run())

    first, second = await asyncio.gather(
        asyncio.to_thread(sync_on_peer, "子任务终态原因 A", "child_failure_a"),
        asyncio.to_thread(sync_on_peer, "子任务终态原因 B", "child_failure_b"),
    )

    assert first.id == workflow.id
    assert second.id == workflow.id
    assert update_count >= 2
    persisted = await workflow_service.get(workflow.id)
    persisted_push = next(
        stage for stage in persisted.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert (persisted_push.reason, persisted_push.error_code) in {
        ("子任务终态原因 A", "child_failure_a"),
        ("子任务终态原因 B", "child_failure_b"),
    }
    assert {first.status, second.status} == {persisted.status}
    await database.engine.dispose()


@pytest.mark.integration
async def test_reusing_task_from_another_workflow_is_rejected(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)

    first_workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            first_workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    existing, reused = await task_service.create(
        "res_magnet", workflow_id=first_workflow.id
    )
    assert reused is False

    second_workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            second_workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )

    with pytest.raises(WorkflowConflict, match="workflow_conflict"):
        await task_service.create("res_magnet", workflow_id=second_workflow.id)

    stored = await task_service.get(existing.id)
    assert stored is not None
    assert stored.workflow_id == first_workflow.id
    first_detail = await workflow_service.get(first_workflow.id)
    first_push = next(
        stage for stage in first_detail.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert first_push.child_id == existing.id
    await database.engine.dispose()


@pytest.mark.integration
async def test_availability_evidence_cannot_advance_another_workflow(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)

    source_workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            source_workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await task_service.create(
        "res_magnet", workflow_id=source_workflow.id
    )
    other_workflow = await workflow_service.create(
        WorkflowCreateRequest(media_type="movie")
    )

    async with database.session_factory() as session:
        stored_task = await session.get(Task, task.id)
        assert stored_task is not None
        with pytest.raises(WorkflowConflict, match="workflow_evidence_required"):
            await record_evidence(
                session,
                workflow_id=source_workflow.id,
                task_id=task.id,
                stage=WorkflowStageName.AVAILABILITY,
                evidence_type="availability_receipt",
                source=EvidenceSource.READONLY_RECONCILIATION,
                subject_id=task.id,
                status=EvidenceStatus.AVAILABLE,
                verified=True,
            )
        stored_task.state = TaskState.AVAILABLE
        evidence = await record_evidence(
            session,
            workflow_id=source_workflow.id,
            task_id=task.id,
            stage=WorkflowStageName.AVAILABILITY,
            evidence_type="availability_receipt",
            source=EvidenceSource.READONLY_RECONCILIATION,
            subject_id=task.id,
            status=EvidenceStatus.AVAILABLE,
            verified=True,
        )
        stored_task.state = TaskState.QUEUED
        with pytest.raises(WorkflowConflict, match="workflow_evidence_required"):
            await advance_availability_from_evidence(
                session, source_workflow.id, task.id, evidence
            )
        stored_task.state = TaskState.AVAILABLE
        with pytest.raises(WorkflowConflict, match="workflow_evidence_required"):
            await advance_availability_from_evidence(
                session, other_workflow.id, task.id, evidence
            )
    await database.engine.dispose()


@pytest.mark.integration
async def test_concurrent_reconciliation_upserts_one_available_evidence(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)

    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await task_service.create("res_magnet", workflow_id=workflow.id)
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        assert stored is not None
        stored.state = TaskState.SUBMITTED
        stored.remote_ref = "remote-concurrent"
        await session.commit()

    adapter = FakeAdapter()
    adapter.remote_status = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )
    results = await asyncio.gather(
        task_service.reconcile(task.id, adapter),
        task_service.reconcile(task.id, adapter),
        return_exceptions=True,
    )

    assert all(not isinstance(result, Exception) for result in results), results
    evidence = await task_service.evidence(task.id)
    assert [item for item in evidence if item.status == "available"]
    assert len([item for item in evidence if item.status == "available"]) == 1
    updated = await task_service.get(task.id)
    assert updated is not None
    assert updated.state is TaskState.AVAILABLE
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_forwards_persisted_target_directory(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet", target_directory_id="314159")

    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="test-worker")
    assert await worker.run_once() is True

    assert adapter.target_cids == ["314159"]
    assert (await service.get(task.id)).target_directory_id == "314159"
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_refreshes_stale_inventory_before_submission(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    guard = RefreshingInventoryGuard(
        InventoryPushCheck(True, "inventory_not_found")
    )
    refreshes = 0

    async def refresh_inventory():
        nonlocal refreshes
        refreshes += 1
        return True

    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="test-worker",
        inventory_guard=guard,
        inventory_refresh=refresh_inventory,
    )

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.SUBMITTED
    assert adapter.submissions == 1
    assert refreshes == 1
    assert guard.checks == 2
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_keeps_stale_inventory_blocked_when_refresh_fails(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    guard = RefreshingInventoryGuard(
        InventoryPushCheck(False, "inventory_index_stale")
    )
    adapter = FakeAdapter()

    async def refresh_inventory():
        return False

    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="test-worker",
        inventory_guard=guard,
        inventory_refresh=refresh_inventory,
    )

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.FAILED
    assert stored.error_code == "inventory_index_stale"
    assert adapter.submissions == 0
    assert guard.checks == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_propagates_failed_refresh_evidence_as_chinese_warning(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    guard = RefreshingInventoryGuard(
        InventoryPushCheck(False, "inventory_index_stale")
    )

    async def refresh_inventory():
        return InventoryRefreshEvidence(
            complete=False,
            scope_verified=True,
            error_code="inventory_index_incomplete",
            library_count=1,
        )

    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="test-worker",
        inventory_guard=guard,
        inventory_refresh=refresh_inventory,
    )

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.FAILED
    assert stored.error_code == "inventory_index_incomplete"
    assert stored.error_message == "媒体库库存扫描不完整，已阻止远端提交。"
    assert adapter.submissions == 0
    assert guard.checks == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_never_refreshes_for_duplicate_inventory(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    guard = RefreshingInventoryGuard(
        InventoryPushCheck(False, "inventory_exact_duplicate"),
        initial_code="inventory_exact_duplicate",
    )
    adapter = FakeAdapter()
    refreshes = 0

    async def refresh_inventory():
        nonlocal refreshes
        refreshes += 1
        return True

    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="test-worker",
        inventory_guard=guard,
        inventory_refresh=refresh_inventory,
    )

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.FAILED
    assert stored.error_code == "inventory_exact_duplicate"
    assert adapter.submissions == 0
    assert refreshes == 0
    assert guard.checks == 1
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("code", "decision"),
    (
        ("inventory_media_duplicate", "media_duplicate"),
        ("inventory_version_duplicate", "version_duplicate"),
        ("inventory_review_required", "needs_review"),
    ),
)
async def test_worker_allows_advisory_inventory_checks(
    tmp_path, code, decision
):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    adapter = FakeAdapter()
    guard = AdvisoryInventoryGuard(code, InventoryDecision(decision))
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="test-worker",
        inventory_guard=guard,
    )

    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.SUBMITTED
    assert adapter.submissions == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_worker_terminal_state_updates_linked_workflow_stage(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    recorder = EventRecorder()
    task_service = TaskService(database.session_factory, event_logger=recorder)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await task_service.create("res_magnet", workflow_id=workflow.id)

    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="workflow-worker",
        event_logger=recorder,
    )
    assert await worker.run_once() is True

    updated = await workflow_service.get(workflow.id)
    push_stage = next(stage for stage in updated.stages if stage.stage.value == "push")
    assert push_stage.status.value == "waiting_external"

    adapter.remote_status = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )
    await task_service.reconcile(task.id, adapter)

    updated = await workflow_service.get(workflow.id)
    push_stage = next(stage for stage in updated.stages if stage.stage.value == "push")
    availability_stage = next(
        stage for stage in updated.stages if stage.stage.value == "availability"
    )
    assert push_stage.status.value == "succeeded"
    assert availability_stage.status.value == "succeeded"
    assert push_stage.child_id == task.id
    assert any(
        event == "workflow.stage_changed"
        and fields["task_id"] == workflow.id
        and fields["correlation_id"] == workflow.correlation_id
        and fields["fields"]["stage"] == "push"
        and fields["fields"]["status"] == "succeeded"
        for event, fields in recorder.events
    )
    await database.engine.dispose()


@pytest.mark.integration
async def test_readonly_reconciliation_resumes_uncertain_workflow_push_stage(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await task_service.create("res_magnet", workflow_id=workflow.id)
    adapter = UncertainReceiptAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="uncertain-recovery-worker",
    )

    assert await worker.run_once() is True
    uncertain = await task_service.get(task.id)
    assert uncertain is not None
    assert uncertain.state is TaskState.UNCERTAIN
    assert uncertain.remote_ref == "remote-recovered"
    before = await workflow_service.get(workflow.id)
    push_before = next(
        stage for stage in before.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert push_before.status is WorkflowStageStatus.UNCERTAIN

    adapter.remote_status = RemoteStatus.ACCEPTED
    reconciled, evidence = await task_service.reconcile(task.id, adapter)

    assert reconciled.state is TaskState.SUBMITTED
    assert evidence.source == EvidenceSource.READONLY_RECONCILIATION.value
    assert evidence.status == EvidenceStatus.SUBMITTED.value
    after = await workflow_service.get(workflow.id)
    push_after = next(
        stage for stage in after.stages if stage.stage is WorkflowStageName.PUSH
    )
    assert push_after.status is WorkflowStageStatus.WAITING_EXTERNAL
    assert push_after.completed_at is None
    await database.engine.dispose()


@pytest.mark.integration
async def test_expired_submitting_task_without_remote_ref_becomes_uncertain(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.SUBMITTING
        stored.lease_owner = "dead-worker"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="new-worker")
    recovered = await worker.recover_expired()
    stored = await service.get(task.id)

    assert recovered == 1
    assert stored.state == TaskState.UNCERTAIN
    assert stored.error_code == "remote_observation_missing"
    assert adapter.submissions == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_local_decryption_failure_is_failed_without_submission(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.encrypted_url_snapshot = "not-valid-ciphertext"
        await session.commit()

    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="test-worker")
    await worker.run_once()
    stored = await service.get(task.id)

    assert stored.state == TaskState.FAILED
    assert stored.error_code == "local_decryption_failed"
    assert adapter.submissions == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_uncertain_submission_emits_actionable_uncertain_event(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")

    class UncertainAdapter(FakeAdapter):
        async def submit_magnet(self, _url: str) -> SubmissionResult:
            raise RuntimeError("remote outcome unavailable")

    recorder = EventRecorder()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        UncertainAdapter(),
        owner="uncertain-worker",
        event_logger=recorder,
    )
    assert await worker.run_once() is True
    stored = await service.get(task.id)
    assert stored.state == TaskState.UNCERTAIN
    event, fields = recorder.events[-1]
    assert event == "task.uncertain"
    assert fields["task_id"] == task.id
    assert fields["resource_id"] == "res_magnet"
    assert fields["fields"]["error_code"] == "adapter_error"
    await database.engine.dispose()


@pytest.mark.integration
async def test_recovery_preserves_confirmed_remote_failure(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.SUBMITTING
        stored.remote_ref = "remote-failed"
        stored.lease_owner = "dead-worker"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    adapter = FakeAdapter()
    adapter.remote_status = RemoteStatus.FAILED
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="new-worker")
    await worker.recover_expired()
    stored = await service.get(task.id)

    assert stored.state == TaskState.FAILED
    await database.engine.dispose()


@pytest.mark.integration
async def test_recovery_bare_available_stays_uncertain(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_magnet")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.SUBMITTING
        stored.remote_ref = "remote-bare-available"
        stored.lease_owner = "dead-worker"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    adapter = FakeAdapter()
    adapter.remote_status = RemoteStatus.AVAILABLE
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="new-worker")

    assert await worker.recover_expired() == 1
    stored = await service.get(task.id)

    assert stored.state == TaskState.UNCERTAIN
    assert stored.error_code == "availability_observation_unverified"
    await database.engine.dispose()


@pytest.mark.integration
async def test_share_task_with_corrupted_snapshot_fails_before_adapter(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_share_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_share")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.encrypted_url_snapshot = "not-a-cookie"
        await session.commit()
    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="test-worker")

    assert await worker.run_once() is True
    stored = await service.get(task.id)

    assert stored.state == TaskState.FAILED
    assert stored.error_code == "local_decryption_failed"
    assert adapter.submissions == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_share_task_is_submitted_through_share_adapter(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_share_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_share")
    adapter = FakeAdapter()
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="test-worker")

    assert await worker.run_once() is True
    stored = await service.get(task.id)

    assert stored.state == TaskState.SUBMITTED
    assert stored.remote_ref == "remote-share"
    assert adapter.submissions == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_expired_share_recovery_reads_status_through_adapter(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_share_resource(database, crypto)
    service = TaskService(database.session_factory)
    task, _ = await service.create("res_share")
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.SUBMITTING
        stored.remote_ref = "share-remote"
        stored.lease_owner = "dead-worker"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    adapter = FakeAdapter()
    adapter.remote_status = RemoteStatus.ACCEPTED
    worker = TaskWorker(database.session_factory, crypto, adapter, owner="new-worker")

    assert await worker.recover_expired() == 1
    stored = await service.get(task.id)

    assert stored.state == TaskState.SUBMITTED
    assert adapter.submissions == 0
    assert adapter.status_lookups == 1
    await database.engine.dispose()


@pytest.mark.integration
async def test_reconcile_rejects_cancelled_task_and_does_not_revive_it(tmp_path):
    """已取消(或取消竞态下)的任务不接受远端核对,不得被复活。"""
    from watch_assistant.schemas import EvidenceSource
    from watch_assistant.services.tasks import (
        TaskNotReconcilable,
        apply_remote_status,
    )
    from watch_assistant.services.workflows import (
        WorkflowCreateRequest,
        WorkflowService,
        WorkflowStageName,
        WorkflowStagePatch,
        WorkflowStageStatus,
    )

    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    task_service = TaskService(database.session_factory)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(
            media_type="movie", tmdb_id=27205, resource_id="res_magnet"
        )
    )
    for stage in (
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await task_service.create("res_magnet", workflow_id=workflow.id)
    adapter = UncertainReceiptAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="cancel-race-worker",
    )
    assert await worker.run_once() is True
    task = await task_service.get(task.id)
    assert task is not None and task.remote_ref is not None

    # 模拟用户取消与迟到观察的竞态:任务已被标记 CANCELLED(带 remote_ref)
    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        stored.state = TaskState.CANCELLED
        stored.error_code = "cancelled"
        stored.updated_at = datetime.now(UTC)
        await session.commit()

    # reconcile 入口直接拒绝
    with pytest.raises(TaskNotReconcilable):
        await task_service.reconcile(task.id, adapter)
    async with database.session_factory() as session:
        still = await session.get(Task, task.id)
    assert still.state is TaskState.CANCELLED
    assert still.error_code == "cancelled"

    # apply_remote_status 的终态守卫:即便绕过入口直接写入也不得复活

    async with database.session_factory() as session:
        stored = await session.get(Task, task.id)
        from watch_assistant.services.tasks import (
            WorkflowConflict,
        )
        with pytest.raises(WorkflowConflict) as conflict:
            await apply_remote_status(
                session,
                stored,
                RemoteStatus.ACCEPTED,
                source=EvidenceSource.READONLY_RECONCILIATION,
            )
        assert str(conflict.value) == "workflow_stage_terminal"
        await session.rollback()
    async with database.session_factory() as session:
        still = await session.get(Task, task.id)
    assert still.state is TaskState.CANCELLED
    await database.engine.dispose()
