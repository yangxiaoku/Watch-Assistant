"""库存重复检测服务单元测试(只读,复用内存 SQLite)。"""

from datetime import UTC, datetime

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.inventory_audit_service import (
    InventoryAuditError,
    InventoryAuditService,
)


@pytest.fixture
async def database(tmp_path):
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'inventory_audit.db'}")
    await initialize_database(db.engine)
    try:
        yield db
    finally:
        await db.engine.dispose()


async def _seed_library(database, *, verified=True):
    now = datetime.now(UTC)
    library = MediaLibrary(
        id="lib_1",
        name="整理归档",
        root_directory_id="root_1",
        scope_verified=verified,
        enabled=True,
        created_at=now,
    )
    async with database.session_factory() as session:
        session.add(library)
        await session.commit()
    return library


async def _seed_scan(database, *, with_entries=True):
    now = datetime.now(UTC)
    run = LibraryScanRun(
        id="run_1",
        library_id="lib_1",
        root_directory_id="root_1",
        idempotency_key="k1",
        state="completed",
        complete=True,
        snapshot_revision=1,
        created_at=now,
        updated_at=now,
    )
    async with database.session_factory() as session:
        session.add(run)
        await session.flush()
        if with_entries:
            session.add_all(
                [
                    LibraryScanEntry(
                        scan_run_id="run_1",
                        object_type="file",
                        object_id="101",
                        parent_id="201",
                        name="某剧.S01E01.1080p.mkv",
                        is_directory=False,
                        size_bytes=1000,
                    ),
                    LibraryScanEntry(
                        scan_run_id="run_1",
                        object_type="file",
                        object_id="102",
                        parent_id="201",
                        name="某剧.S01E01.1080p.mkv",
                        is_directory=False,
                        size_bytes=1000,
                    ),
                ]
            )
        await session.commit()
    return run


async def test_run_audit_detects_duplicate(database):
    await _seed_library(database)
    await _seed_scan(database)
    service = InventoryAuditService(database.session_factory)
    report = await service.run_audit("root_1")
    assert report.duplicate_count == 1
    assert report.reclaimable_bytes == 1000


async def test_run_audit_unverified_library_fails_closed(database):
    await _seed_library(database, verified=False)
    service = InventoryAuditService(database.session_factory)
    with pytest.raises(InventoryAuditError) as exc:
        await service.run_audit("root_1")
    assert exc.value.code == "library_scope_unverified"


async def test_run_audit_no_completed_scan_returns_empty(database):
    await _seed_library(database)
    await _seed_scan(database, with_entries=False)
    # 用未完成扫描替换
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "run_1")
        run.state = "queued"
        run.complete = False
        await session.commit()
    service = InventoryAuditService(database.session_factory)
    report = await service.run_audit("root_1")
    assert report.duplicate_count == 0
    assert report.groups == ()


# ---------- apply_dedupe ----------

from types import SimpleNamespace

from watch_assistant.adapters.p115_library_write_contract import WriteStatus


async def test_apply_dedupe_requires_confirmation(database):
    await _seed_library(database)
    service = InventoryAuditService(database.session_factory)
    with pytest.raises(InventoryAuditError, match="confirmation_required"):
        await service.apply_dedupe("root_1", ["101"], confirm=False)


async def test_apply_dedupe_unavailable_without_transport(database):
    await _seed_library(database)
    service = InventoryAuditService(database.session_factory)
    with pytest.raises(InventoryAuditError, match="dedupe_unavailable"):
        await service.apply_dedupe("root_1", ["101"], confirm=True)


async def test_apply_dedupe_recycles(database, monkeypatch):
    await _seed_library(database)
    scan = await _seed_scan(database)

    async def _verified(session, library):
        return scan

    monkeypatch.setattr(
        "watch_assistant.services.inventory_audit_service.verified_latest_scan",
        _verified,
    )

    class FakeTransport:
        def __init__(self):
            self.executed = []

        async def list_children(self, parent_id, *, timeout_seconds):
            return SimpleNamespace(
                complete=True,
                entries=[
                    SimpleNamespace(
                        file_id="101", is_directory=False, name="某剧.S01E01.1080p.mkv"
                    )
                ],
            )

        async def execute(self, operation, *, timeout_seconds):
            self.executed.append(operation)
            return SimpleNamespace(status=WriteStatus.SUCCESS)

    transport = FakeTransport()
    service = InventoryAuditService(
        database.session_factory, transport_factory=lambda: transport
    )
    deleted, failed = await service.apply_dedupe(
        "root_1", ["101"], confirm=True, operation_delay_seconds=0
    )
    assert deleted == 1
    assert failed == 0
    assert len(transport.executed) == 1
