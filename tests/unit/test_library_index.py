import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryInventoryEvent,
    LibraryObjectLedger,
    LibraryScanCheckpoint,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
    ScanRunState,
    validate_tree_cursor_scope,
)

ROOT_ID = "7000"
LIBRARY_ID = "library-1"
SECRET_NAME = "private-title.mkv"
SECRET_PATH = "/private/cloud/path/private-title.mkv"
SECRET_PICKCODE = "private-pickcode"


class _ReadOnlyGateway:
    def __init__(self, total: int, *, page_size: int = 100):
        self.total = total
        self.page_size = page_size
        self.calls: list[int] = []
        self.cancel_once_page: int | None = None
        self.fail_page: int | None = None
        self.changed_page_count: int | None = None
        self.write_calls = 0

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        assert directory_id == ROOT_ID
        self.calls.append(page)
        if self.cancel_once_page == page:
            self.cancel_once_page = None
            raise asyncio.CancelledError
        if self.fail_page == page:
            raise OSError("private gateway error")
        count = (self.total + page_size - 1) // page_size
        start = (page - 1) * page_size
        if start >= self.total:
            return _page(page, (), count, self.total)
        items = tuple(
            _file_entry(str(index + 1000), path=f"/remote/item-{index}.mkv")
            for index in range(start, min(start + page_size, self.total))
        )
        page_count = self.changed_page_count if page == 2 else count
        return _page(page, items, page_count, self.total)

    def __repr__(self):
        return "ReadOnlyGateway(calls=<redacted>, write_calls=<redacted>)"


class _TreeGateway:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        del page_size
        self.calls.append((directory_id, page))
        if directory_id == ROOT_ID:
            return _page(
                page,
                (
                    LibraryEntry(
                        directory_id="7100",
                        file_id=None,
                        parent_id=ROOT_ID,
                        name="nested",
                        is_directory=True,
                        size_bytes=None,
                        modified_at=None,
                        pickcode=None,
                        path="nested",
                    ),
                    _file_entry("1000", path="/remote/root.mkv"),
                ),
                1,
                2,
            )
        return _page(
            page,
            (_file_entry("1001", path="/remote/nested/item.mkv", parent_id="7100"),),
            1,
            1,
        )


class _PathlessTreeGateway:
    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        del page_size
        if directory_id == ROOT_ID:
            return _page(
                page,
                (
                    LibraryEntry(
                        directory_id="7100",
                        file_id=None,
                        parent_id=ROOT_ID,
                        name="nested",
                        is_directory=True,
                        size_bytes=None,
                        modified_at=None,
                        pickcode=None,
                        path=None,
                    ),
                    replace(_file_entry("1000"), path=None),
                ),
                1,
                2,
            )
        return _page(
            page,
            (replace(_file_entry("1001", parent_id="7100"), path=None),),
            1,
            1,
        )


class _TreeTotalGateway(_TreeGateway):
    def __init__(self, reported_total: int | None):
        super().__init__()
        self.reported_total = reported_total

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        page_value = await super().list_directory(
            directory_id, page=page, page_size=page_size
        )
        if directory_id == ROOT_ID:
            return replace(page_value, total=self.reported_total)
        return page_value


class _RootTotalGateway(_ReadOnlyGateway):
    def __init__(self, reported_total: int | None):
        super().__init__(1)
        self.reported_total = reported_total

    async def list_directory(self, directory_id: str, *, page: int = 1, page_size=100):
        page_value = await super().list_directory(
            directory_id, page=page, page_size=page_size
        )
        return replace(page_value, total=self.reported_total)


def _file_entry(file_id: str, *, path: str = SECRET_PATH, parent_id: str = ROOT_ID):
    return LibraryEntry(
        directory_id=None,
        file_id=file_id,
        parent_id=parent_id,
        name=SECRET_NAME,
        is_directory=False,
        size_bytes=100,
        modified_at=None,
        pickcode=SECRET_PICKCODE,
        path=path,
    )


def _page(page: int, items, page_count: int, total: int, **kwargs):
    return DirectoryPage(
        items=tuple(items),
        page=page,
        page_count=page_count,
        total=total,
        scan_complete=None,
        state=ScanState.COMPLETE,
        **kwargs,
    )


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="private library",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
    return database


def _service(database, gateway, **kwargs):
    return LibraryIndexService(
        database.session_factory,
        gateway,
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_migration_creates_read_index_tables_and_is_idempotent(tmp_path):
    database = await _database(tmp_path)
    await initialize_database(database.engine)
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
    assert {
        "media_libraries",
        "library_scan_runs",
        "library_scan_checkpoints",
        "library_scan_entries",
        "library_scan_diffs",
        "library_object_ledger",
        "library_inventory_events",
    } <= tables
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_large_scan_is_page_streamed_and_persists_complete_snapshot(tmp_path):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(10_000)
    result = await _service(database, gateway, page_size=100).scan("large-scan")

    assert result.state is ScanRunState.COMPLETED
    assert result.complete is True
    assert result.pages_read == 100
    assert result.items_seen == 10_000
    assert result.added_count == 10_000
    assert len(result.changes) == 100
    assert max(gateway.page_size, 100) == 100
    assert gateway.write_calls == 0
    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(LibraryScanEntry))
    assert count == 10_000
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_persists_checkpoint_and_same_key_resumes(tmp_path):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(2, page_size=1)
    gateway.cancel_once_page = 2
    service = _service(database, gateway, page_size=1)

    cancelled = await service.scan("recoverable-scan")
    assert cancelled.state is ScanRunState.CANCELLED
    assert cancelled.complete is False
    assert cancelled.pages_read == 1
    assert cancelled.deletion_candidates == ()
    async with database.session_factory() as session:
        checkpoint = await session.get(LibraryScanCheckpoint, cancelled.run_id)
        assert checkpoint is not None
        assert checkpoint.page == 1

    resumed = await service.scan("recoverable-scan")
    assert resumed.run_id == cancelled.run_id
    assert resumed.state is ScanRunState.COMPLETED
    assert resumed.complete is True
    assert gateway.calls == [1, 2, 2]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_persist_marks_run_cancelled_and_resumes(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(2, page_size=1)
    service = _service(database, gateway, page_size=1)
    original_persist = service._persist_page
    cancelled_once = False

    async def cancel_on_second_page(
        run_id, page, *, expected_page_count, expected_total
    ):
        nonlocal cancelled_once
        if page.page == 2 and not cancelled_once:
            cancelled_once = True
            raise asyncio.CancelledError
        return await original_persist(
            run_id,
            page,
            expected_page_count=expected_page_count,
            expected_total=expected_total,
        )

    monkeypatch.setattr(service, "_persist_page", cancel_on_second_page)
    cancelled = await service.scan("persist-cancel")

    assert cancelled.state is ScanRunState.CANCELLED
    assert cancelled.complete is False
    assert cancelled.error_code == "cancelled"
    assert cancelled.deletion_candidates == ()
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, cancelled.run_id)
        checkpoint = await session.get(LibraryScanCheckpoint, cancelled.run_id)
    assert run is not None
    assert run.state == ScanRunState.CANCELLED.value
    assert run.complete is False
    assert checkpoint is not None
    assert checkpoint.page == 1

    resumed = await service.scan("persist-cancel")
    assert resumed.state is ScanRunState.COMPLETED
    assert resumed.complete is True
    assert gateway.calls == [1, 2, 2]
    assert gateway.write_calls == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_complete_marks_run_cancelled_and_recovers_from_checkpoint(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(1)
    service = _service(database, gateway)
    original_complete = service._complete_run
    cancelled_once = False

    async def cancel_once(run_id):
        nonlocal cancelled_once
        if not cancelled_once:
            cancelled_once = True
            raise asyncio.CancelledError
        return await original_complete(run_id)

    monkeypatch.setattr(service, "_complete_run", cancel_once)
    cancelled = await service.scan("complete-cancel")

    assert cancelled.state is ScanRunState.CANCELLED
    assert cancelled.complete is False
    assert cancelled.error_code == "cancelled"
    assert cancelled.deletion_candidates == ()
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, cancelled.run_id)
        checkpoint = await session.get(LibraryScanCheckpoint, cancelled.run_id)
    assert run is not None
    assert run.state == ScanRunState.CANCELLED.value
    assert run.complete is False
    assert checkpoint is not None
    assert checkpoint.page == 1

    resumed = await service.scan("complete-cancel")
    assert resumed.state is ScanRunState.COMPLETED
    assert resumed.complete is True
    assert gateway.calls == [1]
    assert gateway.write_calls == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_pagination_error_is_incomplete_and_never_deletion_candidate(tmp_path):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(2, page_size=1)
    gateway.changed_page_count = 3
    result = await _service(database, gateway, page_size=1).scan("bad-pages")

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == "page_count_changed"
    assert result.deletion_candidates == ()
    assert result.changes == ()
    assert gateway.write_calls == 0
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_total", "error_code"),
    ((None, "pagination_unverified"), (0, "total_mismatch"), (2, "total_mismatch")),
)
async def test_root_total_evidence_is_fail_closed_before_snapshot_side_effects(
    tmp_path, reported_total, error_code
):
    database = await _database(tmp_path)
    gateway = _RootTotalGateway(reported_total)

    result = await _service(database, gateway).scan(
        f"root-total-{reported_total}"
    )

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == error_code
    assert gateway.calls == [1]
    async with database.session_factory() as session:
        assert await session.scalar(select(LibraryScanEntry)) is None
        assert await session.scalar(select(LibraryScanDiff)) is None
        assert await session.scalar(select(LibraryObjectLedger)) is None
        assert await session.scalar(select(LibraryInventoryEvent)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_external_lease_loss_is_fenced_before_remote_page(tmp_path, monkeypatch):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(1)
    service = _service(
        database,
        gateway,
        lease_owner="worker-one",
        lease_token="lease-token",
    )
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="leased-root-scan",
                library_id=LIBRARY_ID,
                root_directory_id=ROOT_ID,
                idempotency_key="leased-root",
                scan_mode="root",
                state=ScanRunState.RUNNING.value,
                lease_owner="worker-one",
                lease_token="lease-token",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session.flush()
        session.add(LibraryScanCheckpoint(scan_run_id="leased-root-scan"))
        await session.commit()

    original_fence = service._fence_before_remote_page

    async def expire_then_fence(run_id: str):
        async with database.session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            assert run is not None
            run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        await original_fence(run_id)

    monkeypatch.setattr(service, "_fence_before_remote_page", expire_then_fence)
    with pytest.raises(LibraryIndexError, match="lease_claim_lost"):
        await service.scan("leased-root")
    assert gateway.calls == []
    await database.engine.dispose()


@pytest.mark.parametrize("corruption", ("items_seen", "directory_total", "parent_path"))
def test_tree_cursor_evidence_rejects_persisted_scope_corruption(corruption):
    run = LibraryScanRun(
        id="corrupt-cursor",
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        idempotency_key="corrupt-cursor-key",
        scan_mode="tree",
        state=ScanRunState.RUNNING.value,
        expected_total=1,
        pages_read=1,
        items_seen=1,
    )
    checkpoint = LibraryScanCheckpoint(
        scan_run_id=run.id,
        page=1,
        items_seen=1,
        cursor_json=json.dumps(
            {
                "version": 2,
                "directory_totals": {ROOT_ID: 1},
                "expected_total": 1,
                "pending": [
                    {
                        "directory_id": "7100",
                        "parent_path": "nested",
                        "page": 1,
                        "page_count": None,
                        "total": None,
                        "items_seen": 0,
                    }
                ],
                "visited": [ROOT_ID, "7100"],
            }
        ),
    )
    entry = LibraryScanEntry(
        scan_run_id=run.id,
        object_type="directory",
        object_id="7100",
        parent_id=ROOT_ID,
        name="nested",
        path="nested",
        is_directory=True,
    )
    if corruption == "items_seen":
        checkpoint.items_seen = 0
    elif corruption == "directory_total":
        checkpoint.cursor_json = checkpoint.cursor_json.replace('"7000": 1', '"7000": 2')
        run.expected_total = 2
    else:
        checkpoint.cursor_json = checkpoint.cursor_json.replace(
            '"parent_path": "nested"', '"parent_path": "other"'
        )

    with pytest.raises(LibraryIndexError):
        validate_tree_cursor_scope(
            run, checkpoint, (entry,), root_directory_id=ROOT_ID
        )


@pytest.mark.asyncio
async def test_out_of_scope_entry_is_rejected_before_persistence(tmp_path):
    database = await _database(tmp_path)

    class _OutOfScopeGateway(_ReadOnlyGateway):
        async def list_directory(self, directory_id: str, *, page=1, page_size=100):
            page_value = await super().list_directory(
                directory_id, page=page, page_size=page_size
            )
            return replace(
                page_value,
                items=(replace(page_value.items[0], parent_id="9000"),),
            )

    gateway = _OutOfScopeGateway(1)
    result = await _service(database, gateway).scan("scope-entry")

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == "entry_out_of_scope"
    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(LibraryScanEntry))
    assert count == 0
    assert gateway.write_calls == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_parent_traversal_path_is_rejected_before_persistence(tmp_path):
    database = await _database(tmp_path)

    class _UnsafePathGateway(_ReadOnlyGateway):
        async def list_directory(self, directory_id: str, *, page=1, page_size=100):
            page_value = await super().list_directory(
                directory_id, page=page, page_size=page_size
            )
            return replace(
                page_value,
                items=(replace(page_value.items[0], path="../outside.mkv"),),
            )

    result = await _service(database, _UnsafePathGateway(1)).scan("unsafe-path")

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == "entry_path_invalid"
    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(LibraryScanEntry))
    assert count == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_scope_gate_rejects_unverified_library_before_gateway(tmp_path):
    database = await _database(tmp_path)
    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, LIBRARY_ID)
        assert library is not None
        library.scope_verified = False
        await session.commit()
    gateway = _ReadOnlyGateway(1)

    with pytest.raises(LibraryIndexError) as raised:
        await _service(database, gateway).scan("out-of-scope")
    assert raised.value.args == ("library_scope_unverified",)
    assert gateway.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_repeat_snapshot_is_idempotent_and_path_change_uses_stable_identity(
    tmp_path,
):
    database = await _database(tmp_path)
    first_gateway = _ReadOnlyGateway(1)
    first = await _service(database, first_gateway).scan("first")
    repeat = await _service(database, _ReadOnlyGateway(1)).scan("first")
    assert repeat.run_id == first.run_id
    assert repeat.added_count == 1
    assert repeat.changes == first.changes

    class _ChangedPathGateway(_ReadOnlyGateway):
        async def list_directory(self, directory_id: str, *, page=1, page_size=100):
            page_value = await super().list_directory(
                directory_id, page=page, page_size=page_size
            )
            return replace(
                page_value,
                items=tuple(
                    replace(item, path="/remote/new-location.mkv")
                    for item in page_value.items
                ),
            )

    changed = await _service(database, _ChangedPathGateway(1)).scan("second")
    assert changed.state is ScanRunState.COMPLETED
    assert changed.added_count == 0
    assert changed.changed_count == 1
    assert changed.changes[0].change_kind == "changed"
    assert changed.changes[0].path_changed is True
    assert changed.deletion_candidates == ()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_tree_scan_includes_discovered_child_directories(tmp_path):
    database = await _database(tmp_path)
    gateway = _TreeGateway()

    result = await _service(database, gateway, page_size=1).scan_tree("tree-scan")

    assert result.state is ScanRunState.COMPLETED
    assert result.complete is True
    assert result.pages_read == 2
    assert result.items_seen == 3
    assert gateway.calls == [(ROOT_ID, 1), ("7100", 1)]
    async with database.session_factory() as session:
        entries = list(await session.scalars(select(LibraryScanEntry)))
        run = await session.get(LibraryScanRun, result.run_id)
        checkpoint = await session.get(LibraryScanCheckpoint, result.run_id)
    assert {entry.object_id for entry in entries} == {"7100", "1000", "1001"}
    assert run is not None
    assert run.expected_total == 3
    assert checkpoint is not None
    cursor = json.loads(checkpoint.cursor_json)
    assert cursor["directory_totals"] == {ROOT_ID: 2, "7100": 1}
    assert cursor["expected_total"] == 3
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_root_scan_is_recorded_as_non_recursive(tmp_path):
    database = await _database(tmp_path)

    result = await _service(database, _TreeGateway(), page_size=1).scan("root-scan")

    assert result.state is ScanRunState.COMPLETED
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, result.run_id)
    assert run is not None
    assert run.scan_mode == "root"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_tree_scan_materializes_relative_paths_when_gateway_omits_them(tmp_path):
    database = await _database(tmp_path)

    result = await _service(database, _PathlessTreeGateway(), page_size=1).scan_tree(
        "pathless-tree-scan"
    )

    assert result.state is ScanRunState.COMPLETED
    async with database.session_factory() as session:
        entries = list(await session.scalars(select(LibraryScanEntry)))
    paths = {entry.object_id: entry.path for entry in entries}
    assert paths["7100"] == "nested"
    assert paths["1000"] == "private-title.mkv"
    assert paths["1001"] == "nested/private-title.mkv"
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_total", "error_code"),
    ((None, "pagination_unverified"), (1, "total_mismatch"), (3, "total_mismatch")),
)
async def test_tree_total_evidence_is_fail_closed_and_has_no_inventory_side_effects(
    tmp_path, reported_total, error_code
):
    database = await _database(tmp_path)
    baseline = await _service(database, _TreeGateway(), page_size=1).scan_tree(
        "tree-total-baseline"
    )
    assert baseline.complete is True

    result = await _service(
        database, _TreeTotalGateway(reported_total), page_size=1
    ).scan_tree(f"tree-total-{reported_total}")

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == error_code
    assert result.deletion_candidates == ()
    async with database.session_factory() as session:
        entry_count = await session.scalar(
            select(func.count()).where(LibraryScanEntry.scan_run_id == result.run_id)
        )
        diff_count = await session.scalar(
            select(func.count()).where(LibraryScanDiff.scan_run_id == result.run_id)
        )
        ledger_count = await session.scalar(
            select(func.count()).select_from(LibraryObjectLedger)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(LibraryInventoryEvent)
        )
    assert entry_count == 0
    assert diff_count == 0
    assert ledger_count == 3
    assert event_count == 3
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_complete_scan_records_removal_and_restoration_ledger(tmp_path):
    database = await _database(tmp_path)
    first = await _service(database, _ReadOnlyGateway(1)).scan("present")
    assert first.complete is True

    class _EmptyGateway(_ReadOnlyGateway):
        async def list_directory(self, directory_id: str, *, page=1, page_size=100):
            assert directory_id == ROOT_ID
            return _page(
                page,
                (),
                1,
                0,
                terminal=True,
                has_more=False,
            )

    removed = await _service(database, _EmptyGateway(0)).scan("removed")
    assert removed.complete is True
    assert removed.removed_count == 1
    assert removed.deletion_candidates == ("1000",)
    async with database.session_factory() as session:
        ledger = await session.scalar(select(LibraryObjectLedger))
        events = list((await session.scalars(select(LibraryInventoryEvent))).all())
    assert ledger is not None
    assert ledger.status == "missing"
    assert [event.event_kind for event in events] == ["added", "removed"]

    restored = await _service(database, _ReadOnlyGateway(1)).scan("restored")
    assert restored.complete is True
    assert restored.deletion_candidates == ()
    async with database.session_factory() as session:
        ledger = await session.scalar(select(LibraryObjectLedger))
        events = list(
            (
                await session.scalars(
                    select(LibraryInventoryEvent).order_by(LibraryInventoryEvent.created_at)
                )
            ).all()
        )
    assert ledger is not None
    assert ledger.status == "active"
    assert [event.event_kind for event in events] == ["added", "removed", "restored"]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_same_complete_snapshot_under_new_key_has_no_new_changes(tmp_path):
    database = await _database(tmp_path)
    first = await _service(database, _ReadOnlyGateway(1)).scan("snapshot-a")
    second = await _service(database, _ReadOnlyGateway(1)).scan("snapshot-b")

    assert first.added_count == 1
    assert second.added_count == 0
    assert second.changed_count == 0
    assert second.changes == ()
    assert second.complete is True
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_gateway_failure_keeps_checkpoint_and_redacts_result(tmp_path):
    database = await _database(tmp_path)
    gateway = _ReadOnlyGateway(2, page_size=1)
    gateway.fail_page = 2
    result = await _service(database, gateway, page_size=1).scan("failed-scan")

    assert result.state is ScanRunState.FAILED
    assert result.complete is False
    assert result.error_code == "gateway_error"
    rendered = repr(result) + repr(gateway) + repr(result.to_public_dict())
    for secret in (SECRET_NAME, SECRET_PATH, SECRET_PICKCODE, ROOT_ID, LIBRARY_ID):
        assert secret not in rendered
    await database.engine.dispose()
