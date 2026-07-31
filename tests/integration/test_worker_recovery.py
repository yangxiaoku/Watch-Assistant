from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import (
    RemoteStatus,
    SubmissionResult,
    WorkflowCreateRequest,
)
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


class EventRecorder:
    def __init__(self):
        self.events = []

    async def log_event(self, event, **kwargs):
        self.events.append((event, kwargs))


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
    assert task.state == TaskState.ACCEPTED
    assert task.remote_ref == "remote-123"
    assert adapter.submissions == 1
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
async def test_worker_terminal_state_updates_linked_workflow_stage(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    recorder = EventRecorder()
    task_service = TaskService(database.session_factory, event_logger=recorder)
    workflow_service = WorkflowService(database.session_factory)
    workflow = await workflow_service.create(
        WorkflowCreateRequest(media_type="movie", tmdb_id=27205)
    )
    task, _ = await task_service.create("res_magnet", workflow_id=workflow.id)

    worker = TaskWorker(
        database.session_factory,
        crypto,
        FakeAdapter(),
        owner="workflow-worker",
        event_logger=recorder,
    )
    assert await worker.run_once() is True

    updated = await workflow_service.get(workflow.id)
    push_stage = next(stage for stage in updated.stages if stage.stage.value == "push")
    assert push_stage.status.value == "succeeded"
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
