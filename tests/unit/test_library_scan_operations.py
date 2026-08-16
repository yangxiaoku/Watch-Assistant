import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryInventoryEvent,
    LibraryObjectLedger,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
)
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
    LibraryScanWorker,
)

ROOT_ID = "7000"
LIBRARY_ID = "library-scan"


class _EventLogger:
    def __init__(self):
        self.events = []

    async def log_event(self, event, **kwargs):
        self.events.append((event, kwargs.get("fields")))


class _Gateway:
    def __init__(
        self,
        *,
        fail_once: bool = False,
        interrupt_page: int | None = None,
        total: int = 2,
    ):
        self.fail_once = fail_once
        self.interrupt_page = interrupt_page
        self.total = total
        self.calls: list[int] = []

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        assert directory_id == ROOT_ID
        assert page_size == 50
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
            total=self.total,
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
    now = datetime.now(UTC)

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
    logger = _EventLogger()
    service = LibraryScanOperationService(
        database.session_factory, event_logger=logger
    )
    gateway = _Gateway(fail_once=True, total=2)
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id, _scope: gateway,
        owner="scan-worker",
        poll_interval_seconds=0.01,
        event_logger=logger,
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
    assert logger.events == [
        ("library.scan.queued", {"status": "queued", "count": 1}),
        ("library.scan.failed", {"status": "failed", "error_code": "gateway_error"}),
        ("library.scan.queued", {"status": "queued", "count": 1}),
        ("library.scan.completed", {"status": "completed", "items_seen": 2}),
    ]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_direct_scan_cannot_steal_failed_worker_lease_before_release(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    gateway = _Gateway(fail_once=True)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="failed-lease")
    now = datetime.now(UTC)
    old_lease = await service.claim_next(
        owner="old-worker",
        lease_duration=timedelta(minutes=1),
        now=now,
    )
    assert old_lease is not None

    old_scan = LibraryIndexService(
        database.session_factory,
        gateway,
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
        lease_owner=old_lease.lease_owner,
        lease_token=old_lease.lease_token,
    )
    failed = await old_scan.scan_tree("failed-lease")
    assert failed.state.value == "failed"
    assert failed.complete is False

    direct = LibraryIndexService(
        database.session_factory,
        gateway,
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
    )
    blocked = await direct.scan_tree("failed-lease")

    assert blocked.state.value == "failed"
    assert blocked.complete is False
    assert gateway.calls == [1]
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
    assert run is not None
    assert run.lease_token == old_lease.lease_token
    assert run.lease_owner == old_lease.lease_owner
    await service.release(old_lease, error_code=failed.error_code)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_worker_interruption_requeues_with_durable_tree_cursor(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    gateway = _Gateway(interrupt_page=2, total=2)
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id, _scope: gateway,
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
        page_size=50,
        page_delay_seconds=0,
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
        lambda _root_id, _scope: _SlowGateway(),
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
    now = datetime.now(UTC)
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
            assert page_size == 50
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

    lease_lost_event = asyncio.Event()
    scan = LibraryIndexService(
        database.session_factory,
        _BlockingGateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
        propagate_cancelled=True,
        cancel_event=lease_lost_event,
        lease_owner=old_lease.lease_owner,
        lease_token=old_lease.lease_token,
    )
    task = asyncio.create_task(scan.scan_tree("reclaimed-lease"))
    await gateway_started.wait()

    recovery_time = now + timedelta(minutes=2)
    assert await service.recover_expired(now=recovery_time) == 1
    new_lease = await service.claim_next(owner="new-worker", now=recovery_time)
    assert new_lease is not None
    assert new_lease.lease_token != old_lease.lease_token

    lease_lost_event.set()
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


@pytest.mark.asyncio
async def test_reclaimed_lease_cannot_persist_a_page_returned_late(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="late-page")
    now = datetime.now(UTC)
    old_lease = await service.claim_next(
        owner="old-worker",
        lease_duration=timedelta(minutes=1),
        now=now,
    )
    assert old_lease is not None
    page_started = asyncio.Event()
    return_page = asyncio.Event()

    class _LatePageGateway:
        async def list_directory(
            self, directory_id: str, *, page: int = 1, page_size=100
        ):
            assert directory_id == ROOT_ID
            assert page == 1
            assert page_size == 50
            page_started.set()
            await return_page.wait()
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
        _LatePageGateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
        lease_owner=old_lease.lease_owner,
        lease_token=old_lease.lease_token,
    )
    task = asyncio.create_task(scan.scan_tree("late-page"))
    await page_started.wait()

    recovery_time = now + timedelta(minutes=2)
    assert await service.recover_expired(now=recovery_time) == 1
    new_lease = await service.claim_next(owner="new-worker", now=recovery_time)
    assert new_lease is not None
    return_page.set()

    with pytest.raises(LibraryIndexError, match="lease_claim_lost"):
        await task

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
        count = await session.scalar(
            select(func.count()).select_from(LibraryScanEntry)
        )
        diff_count = await session.scalar(
            select(func.count())
            .select_from(LibraryScanDiff)
            .where(LibraryScanDiff.scan_run_id == queued.run_id)
        )
        ledger_count = await session.scalar(
            select(func.count()).select_from(LibraryObjectLedger)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(LibraryInventoryEvent)
        )
    assert run is not None
    assert run.state == "running"
    assert run.lease_token == new_lease.lease_token
    assert run.pages_read == 0
    assert count == 0
    assert run.complete is False
    assert diff_count == 0
    assert ledger_count == 0
    assert event_count == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_final_lease_fence_rolls_back_inventory_and_completion(tmp_path, monkeypatch):
    database = await _database(tmp_path)
    scan = LibraryIndexService(
        database.session_factory,
        _Gateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
    )
    original_fence = scan._fence_write

    async def fail_after_complete(session, run, *, refresh=True):
        if not refresh and not run.complete and run.snapshot_revision is not None:
            raise LibraryIndexError("lease_claim_lost")
        await original_fence(session, run, refresh=refresh)

    monkeypatch.setattr(scan, "_fence_write", fail_after_complete)

    with pytest.raises(LibraryIndexError, match="lease_claim_lost"):
        await scan.scan_tree("final-fence")

    async with database.session_factory() as session:
        run = await session.scalar(select(LibraryScanRun))
        entry_count = await session.scalar(
            select(func.count()).select_from(LibraryScanEntry)
        )
        diff_count = await session.scalar(
            select(func.count()).select_from(LibraryScanDiff)
        )
        ledger_count = await session.scalar(
            select(func.count()).select_from(LibraryObjectLedger)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(LibraryInventoryEvent)
        )
    assert run is not None
    assert run.complete is False
    assert run.state == "running"
    assert entry_count == 2
    assert diff_count == 0
    assert ledger_count == 0
    assert event_count == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_direct_scans_share_one_execution_lease(tmp_path):
    database = await _database(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowGateway:
        def __init__(self):
            self.calls = 0

        async def list_directory(
            self, directory_id: str, *, page: int = 1, page_size=100
        ):
            assert directory_id == ROOT_ID
            assert page == 1
            assert page_size == 50
            self.calls += 1
            started.set()
            await release.wait()
            return DirectoryPage(
                items=(
                    LibraryEntry(
                        directory_id=None,
                        file_id="1001",
                        parent_id=ROOT_ID,
                        name="title.mkv",
                        is_directory=False,
                        size_bytes=1,
                        modified_at=None,
                        pickcode=None,
                        path="title.mkv",
                    ),
                ),
                page=1,
                page_count=1,
                total=1,
                scan_complete=True,
                state=ScanState.COMPLETE,
                has_more=False,
                terminal=True,
            )

    first_gateway = _SlowGateway()
    first = LibraryIndexService(
        database.session_factory,
        first_gateway,
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
    )
    second = LibraryIndexService(
        database.session_factory,
        first_gateway,
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
        page_delay_seconds=0,
    )
    first_task = asyncio.create_task(first.scan_tree("same-direct-key"))
    await started.wait()
    second_result = await second.scan_tree("same-direct-key")
    assert second_result.state.value == "running"
    assert first_gateway.calls == 1

    release.set()
    first_result = await first_task
    assert first_result.complete is True
    repeated = await second.scan_tree("same-direct-key")
    assert repeated.complete is True
    async with database.session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(LibraryScanEntry))
            == 1
        )
    await database.engine.dispose()


class _CancelGateway:
    """快照分页被 115 服务端取消(活跃目录并发修改的典型表现)。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        self.calls.append(page)
        return DirectoryPage(
            items=(),
            page=page,
            page_count=2,
            total=0,
            scan_complete=False,
            state=ScanState.CANCELLED,
            has_more=True,
            terminal=False,
        )


@pytest.mark.asyncio
async def test_repeated_gateway_cancel_hits_requeue_limit_instead_of_hot_loop(tmp_path):
    """修复前:网关 CANCELLED 无限 requeue,扫描永不完成且满速热循环。"""
    from watch_assistant.services.library_scan_operations import (
        MAX_SCAN_REQUEUE_ATTEMPTS,
    )

    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    gateway = _CancelGateway()
    worker = LibraryScanWorker(
        database.session_factory,
        service,
        lambda _root_id, _scope: gateway,
        owner="cancel-loop-worker",
        poll_interval_seconds=0.01,
    )
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="cancel-loop")

    worked = 0
    while worked < MAX_SCAN_REQUEUE_ATTEMPTS + 2:
        if not await worker.run_once():
            break
        worked += 1

    final = await service.get(LIBRARY_ID, queued.run_id)
    assert final.state == "failed"
    assert final.error_code == "scan_requeue_limit"
    assert final.attempts == MAX_SCAN_REQUEUE_ATTEMPTS
    assert worked == MAX_SCAN_REQUEUE_ATTEMPTS  # 之后 claim 不到任务,不再空转
    await database.engine.dispose()
