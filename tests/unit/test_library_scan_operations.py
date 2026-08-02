import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import LibraryScanEntry, MediaLibrary
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
    LibraryScanWorker,
)

ROOT_ID = "7000"
LIBRARY_ID = "library-scan"


class _Gateway:
    def __init__(self, *, fail_once: bool = False, interrupt_page: int | None = None):
        self.fail_once = fail_once
        self.interrupt_page = interrupt_page
        self.calls: list[int] = []

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        assert directory_id == ROOT_ID
        assert page_size == 1
        self.calls.append(page)
        if self.fail_once:
            self.fail_once = False
            raise OSError("gateway failure")
        if self.interrupt_page == page:
            self.interrupt_page = None
            raise asyncio.CancelledError
        return DirectoryPage(
            items=(
                LibraryEntry(
                    directory_id=None,
                    file_id=str(1000 + page),
                    parent_id=ROOT_ID,
                    name=f"title-{page}.mkv",
                    is_directory=False,
                    size_bytes=100,
                    modified_at=None,
                    pickcode=None,
                    path=f"title-{page}.mkv",
                ),
            ),
            page=page,
            page_count=2,
            total=2,
            scan_complete=True,
            state=ScanState.COMPLETE,
            has_more=page == 1,
            terminal=page == 2,
        )


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="library",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
    return database


@pytest.mark.asyncio
async def test_active_idempotent_enqueue_does_not_steal_lease_and_expired_lease_recovers(
    tmp_path,
):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    queued = await service.enqueue(LIBRARY_ID, idempotency_key="same-key")
    first_lease = await service.claim_next(
        owner="worker-one",
        lease_duration=timedelta(minutes=1),
        now=now,
    )
    assert first_lease is not None

    active_retry = await service.enqueue(
        LIBRARY_ID,
        idempotency_key="same-key",
        max_directories=2,
    )
    assert active_retry.run_id == queued.run_id
    assert active_retry.state == "running"
    assert active_retry.attempts == 1
    assert active_retry.cancel_requested is False

    assert await service.recover_expired(now=now + timedelta(minutes=2)) == 1
    recovered = await service.get(LIBRARY_ID, queued.run_id)
    assert recovered.state == "queued"
    assert recovered.error_code == "scan_worker_recovered"

    second_lease = await service.claim_next(
        owner="worker-two",
        lease_duration=timedelta(minutes=1),
        now=now + timedelta(minutes=2),
    )
    assert second_lease is not None
    retried = await service.get(LIBRARY_ID, queued.run_id)
    assert retried.attempts == 2
    assert retried.state == "running"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_worker_failure_is_persisted_then_same_key_requeues_and_completes(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    gateway = _Gateway(fail_once=True)
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id: gateway,
        owner="scan-worker",
        poll_interval_seconds=0.01,
    )
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="recoverable")

    assert await worker.run_once() is True
    failed = await service.get(LIBRARY_ID, queued.run_id)
    assert failed.state == "failed"
    assert failed.error_code == "gateway_error"
    assert failed.pages_read == 0
    assert failed.attempts == 1

    await service.enqueue(LIBRARY_ID, idempotency_key="recoverable")
    assert await worker.run_once() is True
    completed = await service.get(LIBRARY_ID, failed.run_id)
    assert completed.state == "completed"
    assert completed.complete is True
    assert completed.attempts == 2
    assert gateway.calls == [1, 1, 2]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_worker_interruption_requeues_with_durable_tree_cursor(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    gateway = _Gateway(interrupt_page=2)
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id: gateway,
        owner="scan-worker",
        poll_interval_seconds=0.01,
    )
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="cursor-key")

    assert await worker.run_once() is True
    interrupted = await service.get(LIBRARY_ID, queued.run_id)
    assert interrupted.state == "queued"
    assert interrupted.error_code == "scan_worker_recovered"
    assert interrupted.pages_read == 1
    assert interrupted.items_seen == 1

    await service.enqueue(LIBRARY_ID, idempotency_key="cursor-key")
    assert await worker.run_once() is True
    completed = await service.get(LIBRARY_ID, queued.run_id)
    assert completed.state == "completed"
    assert completed.complete is True
    assert completed.pages_read == 2
    assert completed.items_seen == 2
    assert gateway.calls == [1, 2, 2]
    async with database.session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(LibraryScanEntry)
        )
    assert count == 2
    await database.engine.dispose()
