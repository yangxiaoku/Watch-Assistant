import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_index import LibraryIndexService
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


@pytest.mark.asyncio
async def test_concurrent_enqueue_same_key_returns_one_persisted_run(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)

    summaries = await asyncio.gather(
        service.enqueue(LIBRARY_ID, idempotency_key="concurrent-key"),
        service.enqueue(LIBRARY_ID, idempotency_key="concurrent-key"),
    )

    assert len({summary.run_id for summary in summaries}) == 1
    async with database.session_factory() as session:
        runs = list((await session.scalars(select(LibraryScanRun))).all())
    assert len(runs) == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claim_next_grants_only_one_lease(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    await service.enqueue(LIBRARY_ID, idempotency_key="claim-key")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    leases = await asyncio.gather(
        service.claim_next(
            owner="worker-one",
            lease_duration=timedelta(minutes=1),
            now=now,
        ),
        service.claim_next(
            owner="worker-two",
            lease_duration=timedelta(minutes=1),
            now=now,
        ),
    )

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    run = await service.get(LIBRARY_ID, claimed[0].run_id)
    assert run.state == "running"
    assert run.attempts == 1
    assert run.error_code is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_tree_reset_preserves_the_active_lease(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="reset-key")
    lease = await service.claim_next(owner="scan-worker")
    assert lease is not None

    index = LibraryIndexService(
        database.session_factory,
        _Gateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=1,
    )
    await index._reset_tree_run(queued.run_id)

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
    assert run is not None
    assert run.state == "running"
    assert run.lease_owner == lease.lease_owner
    assert run.lease_token == lease.lease_token
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_worker_stops_without_cancelled_write_when_lease_renewal_is_lost(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)

    class _SlowGateway(_Gateway):
        async def list_directory(
            self, directory_id: str, *, page: int = 1, page_size=100
        ):
            await asyncio.sleep(1)
            return await super().list_directory(
                directory_id, page=page, page_size=page_size
            )

    async def lose_lease(*_args, **_kwargs):
        return False

    monkeypatch.setattr(service, "renew", lose_lease)
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id: _SlowGateway(),
        owner="scan-worker",
        lease_duration=timedelta(seconds=0.3),
    )
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="lease-lost")

    assert await worker.run_once() is True
    summary = await service.get(LIBRARY_ID, queued.run_id)
    assert summary.state == "failed"
    assert summary.error_code == "lease_claim_lost"
    assert summary.error_message_zh is not None
    assert "扫描执行权" in summary.error_message_zh
    assert summary.pages_read == 0
    async with database.session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(LibraryScanEntry)
        )
    assert count == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_scan_cannot_overwrite_a_reclaimed_lease(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="reclaimed-lease")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    old_lease = await service.claim_next(
        owner="old-worker",
        lease_duration=timedelta(minutes=1),
        now=now,
    )
    assert old_lease is not None
    gateway_started = asyncio.Event()

    class _BlockingGateway:
        async def list_directory(
            self, directory_id: str, *, page: int = 1, page_size=100
        ):
            assert directory_id == ROOT_ID
            assert page == 1
            assert page_size == 1
            gateway_started.set()
            await asyncio.sleep(10)
            return DirectoryPage(
                items=(),
                page=1,
                page_count=1,
                total=0,
                scan_complete=True,
                state=ScanState.COMPLETE,
                has_more=False,
                terminal=True,
            )

    scan = LibraryIndexService(
        database.session_factory,
        _BlockingGateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=1,
        propagate_cancelled=True,
    )
    task = asyncio.create_task(scan.scan_tree("reclaimed-lease"))
    await gateway_started.wait()

    recovery_time = now + timedelta(minutes=2)
    assert await service.recover_expired(now=recovery_time) == 1
    new_lease = await service.claim_next(owner="new-worker", now=recovery_time)
    assert new_lease is not None
    assert new_lease.lease_token != old_lease.lease_token

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    summary = await service.get(LIBRARY_ID, queued.run_id)
    assert summary.state == "running"
    assert summary.error_code is None
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
    assert run is not None
    assert run.lease_owner == "new-worker"
    assert run.lease_token == new_lease.lease_token
    await database.engine.dispose()
