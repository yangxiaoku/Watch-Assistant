from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_organization_operations import _database, _operation

from watch_assistant.models import DirectoryDirtyEvent, OrganizationOperationStatus
from watch_assistant.services.directory_dirty_worker import DirectoryDirtyWorker
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
)


async def _claimed(database):
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    lease = await service.claim(operation.operation_id, expected_revision=1)
    return service, operation, lease


class _FakeIndex:
    def __init__(self):
        self.calls = []

    async def scan_tree(self, key):
        self.calls.append(key)
        return SimpleNamespace(complete=True, run_id="scan-dirty-result")


class _FakeStrm:
    def __init__(self):
        self.calls = []

    async def incremental(self, library_id, **kwargs):
        self.calls.append((library_id, kwargs))


@pytest.mark.asyncio
async def test_dirty_worker_consumes_event_and_preserves_cleanup_gate(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    async with database.session_factory() as session:
        event = await session.scalar(
            select(DirectoryDirtyEvent)
            .where(DirectoryDirtyEvent.directory_id == "7000")
            .order_by(DirectoryDirtyEvent.id)
        )
        assert event is not None

    index = _FakeIndex()
    strm = _FakeStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        strm,
        lambda library_id, root_id: index,
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        cleanup_enabled=False,
    )
    assert await worker.run_once()
    assert len(index.calls) == 1
    assert len(strm.calls) == 1
    assert strm.calls[0][1]["retire_removed"] is False
    async with database.session_factory() as session:
        current = await session.get(DirectoryDirtyEvent, event.id)
        assert current is not None
        assert current.status == "consumed"
        assert current.error_code is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_retries_incomplete_scan(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    class _IncompleteIndex:
        async def scan_tree(self, _key):
            return SimpleNamespace(complete=False, run_id="scan-incomplete")

    worker = DirectoryDirtyWorker(
        database.session_factory,
        _FakeStrm(),
        lambda _library_id, _root_id: _IncompleteIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
    )
    assert await worker.run_once()
    async with database.session_factory() as session:
        row = await session.scalar(select(DirectoryDirtyEvent))
        assert row is not None
        assert row.status == "pending"
        assert row.error_code == "scan_incomplete"
        assert row.attempts == 1
    await database.engine.dispose()
