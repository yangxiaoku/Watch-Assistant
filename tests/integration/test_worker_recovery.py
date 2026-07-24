from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import RemoteStatus, SubmissionResult
from watch_assistant.services.tasks import TaskService
from watch_assistant.worker import TaskWorker


class FakeAdapter:
    def __init__(self):
        self.submissions = 0
        self.remote_status = None

    async def submit_magnet(self, url: str) -> SubmissionResult:
        self.submissions += 1
        assert url.startswith("magnet:")
        return SubmissionResult(
            status=RemoteStatus.ACCEPTED, remote_ref="remote-123"
        )

    async def save_share(self, url: str, password: str | None) -> SubmissionResult:
        self.submissions += 1
        return SubmissionResult(
            status=RemoteStatus.ACCEPTED, remote_ref="remote-share"
        )

    async def get_status(self, remote_ref: str):
        return self.remote_status


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
