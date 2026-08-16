"""SmallFileCleanupService: preview + recycle (recoverable)."""

from types import SimpleNamespace

import pytest

from watch_assistant.adapters.p115_library_write_contract import WriteStatus
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.small_file_cleanup import (
    SmallFileCleanupError,
    SmallFileCleanupService,
)

_THRESHOLD = 100 * 1024 * 1024  # 100MB


async def _setup(tmp_path):
    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'small-cleanup.db'}"
    )
    await initialize_database(database.engine)
    library = MediaLibrary(
        id="library-1",
        name="library",
        root_directory_id="1",
        enabled=True,
        scope_verified=True,
    )
    scan = LibraryScanRun(
        id="scan-1",
        library_id=library.id,
        root_directory_id="1",
        idempotency_key="scan-key-1",
        state="completed",
        complete=True,
        snapshot_revision=1,
    )
    entries = [
        LibraryScanEntry(
            scan_run_id="scan-1",
            object_type="file",
            object_id="101",
            parent_id="201",
            name="small-1.nfo",
            size_bytes=1_000,
            is_directory=False,
        ),
        LibraryScanEntry(
            scan_run_id="scan-1",
            object_type="file",
            object_id="102",
            parent_id="201",
            name="small-2.bin",
            size_bytes=50 * 1024 * 1024,
            is_directory=False,
        ),
        LibraryScanEntry(
            scan_run_id="scan-1",
            object_type="file",
            object_id="103",
            parent_id="201",
            name="big-1.mkv",
            size_bytes=2 * 1024 * 1024 * 1024,
            is_directory=False,
        ),
        LibraryScanEntry(
            scan_run_id="scan-1",
            object_type="directory",
            object_id="201",
            parent_id="1",
            name="201",
            size_bytes=None,
            is_directory=True,
        ),
    ]
    async with database.session_factory() as session:
        session.add(library)
        await session.flush()
        session.add(scan)
        await session.flush()
        session.add_all(entries)
        await session.commit()
    return database, library, scan


@pytest.mark.asyncio
async def test_preview_returns_only_small_files_from_completed_snapshot(tmp_path, monkeypatch):
    database, library, scan = await _setup(tmp_path)
    async def _verified(session, library):
        return scan
    monkeypatch.setattr("watch_assistant.services.small_file_cleanup.verified_latest_scan", _verified)
    try:
        service = SmallFileCleanupService(database.session_factory)
        scan, candidates = await service.preview(
            library, threshold_bytes=_THRESHOLD
        )
        assert scan.id == "scan-1"
        ids = {c.file_id for c in candidates}
        assert ids == {"101", "102"}
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_preview_rejects_zero_threshold(tmp_path):
    database, library, _scan = await _setup(tmp_path)
    try:
        service = SmallFileCleanupService(database.session_factory)
        with pytest.raises(SmallFileCleanupError, match="small_file_threshold_unconfigured"):
            await service.preview(library, threshold_bytes=0)
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_preview_rejects_missing_snapshot(tmp_path, monkeypatch):
    database, _library, _scan = await _setup(tmp_path)
    async def _none(session, library):
        return None
    monkeypatch.setattr("watch_assistant.services.small_file_cleanup.verified_latest_scan", _none)
    try:
        service = SmallFileCleanupService(database.session_factory)
        library = MediaLibrary(
            id="other",
            name="other",
            root_directory_id="9",
            enabled=True,
            scope_verified=True,
        )
        with pytest.raises(SmallFileCleanupError, match="cleanup_snapshot_unavailable"):
            await service.preview(library, threshold_bytes=_THRESHOLD)
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_apply_recycles_verified_files_and_skips_moved(tmp_path):
    database, library, _scan = await _setup(tmp_path)

    class _FakeTransport:
        def __init__(self):
            self.executed: list[object] = []

        async def list_children(self, parent_id, *, timeout_seconds):
            # 实时目录:small-1 仍在,small-2 已消失(应被跳过)。
            return SimpleNamespace(
                complete=True,
                entries=[
                    SimpleNamespace(
                        file_id="101",
                        is_directory=False,
                        name="small-1.nfo",
                    ),
                    SimpleNamespace(
                        file_id="999",
                        is_directory=False,
                        name="small-2.bin",
                    ),
                ],
            )

        async def execute(self, operation, *, timeout_seconds):
            self.executed.append(operation)
            return SimpleNamespace(status=WriteStatus.SUCCESS)

    transport = _FakeTransport()
    service = SmallFileCleanupService(
        database.session_factory, transport_factory=lambda: transport
    )
    try:
        deleted, failed = await service.apply(
            library,
            scan_run_id="scan-1",
            file_ids=["101", "102"],
            confirm=True,
            operation_delay_seconds=0,
        )
        assert deleted == 1
        assert failed == 1
        assert len(transport.executed) == 1  # 只有 small-1 通过重验证
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_apply_requires_confirmation(tmp_path):
    database, library, _scan = await _setup(tmp_path)
    try:
        service = SmallFileCleanupService(database.session_factory)
        with pytest.raises(SmallFileCleanupError, match="confirmation_required"):
            await service.apply(
                library,
                scan_run_id="scan-1",
                file_ids=["101"],
                confirm=False,
                operation_delay_seconds=0,
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_apply_falls_back_to_snapshot_identity_when_listing_incomplete(tmp_path):
    """大目录降级:实时目录列表不完整(115 C03 分页上限)时按已验证快照身份
    直接回收站删除,文件已不存在时由 115 侧 prepare_delete 返回失败。"""
    database, library, _scan = await _setup(tmp_path)

    class _FakeTransport:
        def __init__(self):
            self.executed: list[object] = []

        async def list_children(self, parent_id, *, timeout_seconds):
            # 目录条目超过分页上限:列表不完整且未覆盖到候选 id。
            return SimpleNamespace(complete=False, entries=[])

        async def execute(self, operation, *, timeout_seconds):
            self.executed.append(operation)
            return SimpleNamespace(status=WriteStatus.SUCCESS)

    transport = _FakeTransport()
    service = SmallFileCleanupService(
        database.session_factory, transport_factory=lambda: transport
    )
    try:
        deleted, failed = await service.apply(
            library,
            scan_run_id="scan-1",
            file_ids=["101", "102"],
            confirm=True,
            operation_delay_seconds=0,
        )
        assert deleted == 2
        assert failed == 0
        assert len(transport.executed) == 2
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_apply_skips_renamed_file_seen_in_partial_listing(tmp_path):
    """部分列表里已看到候选 id 且名字不符 → 快照身份失效,禁止删除(fail-closed)。"""
    database, library, _scan = await _setup(tmp_path)

    class _FakeTransport:
        async def list_children(self, parent_id, *, timeout_seconds):
            return SimpleNamespace(
                complete=False,
                entries=[
                    SimpleNamespace(
                        file_id="101",
                        is_directory=False,
                        name="renamed.nfo",
                    )
                ],
            )

        async def execute(self, operation, *, timeout_seconds):
            raise AssertionError("execute must not be called")

    service = SmallFileCleanupService(
        database.session_factory, transport_factory=lambda: _FakeTransport()
    )
    try:
        deleted, failed = await service.apply(
            library,
            scan_run_id="scan-1",
            file_ids=["101"],
            confirm=True,
            operation_delay_seconds=0,
        )
        assert deleted == 0
        assert failed == 1
    finally:
        await database.engine.dispose()
