import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import (
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
from watch_assistant.services.workflows import WorkflowService
from watch_assistant.worker import TaskWorker


class FakeAdapter:
    def __init__(self):
        self.submissions = 0
        self.target_cids = []
        self.remote_status = None
        self.status_lookups = 0

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

    async def get_status(self, remote_ref: str):
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

    adapter = FakeAdapter()
    worker = TaskWorker(
        database.session_factory,
        crypto,
        adapter,
        owner="workflow-worker",
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
async def test_queued_share_is_failed_without_calling_share_adapter(tmp_path):
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
    assert stored.error_code == "push_kind_unsupported"
    assert adapter.submissions == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_expired_share_is_failed_without_status_lookup(tmp_path):
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

    assert stored.state == TaskState.FAILED
    assert stored.error_code == "push_kind_unsupported"
    assert adapter.submissions == 0
    assert adapter.status_lookups == 0
    await database.engine.dispose()
