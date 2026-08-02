import asyncio
import json
from pathlib import Path

import pytest

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
)
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
    LibraryScanWorker,
)

ROOT_ID = "7000"
CHILD_ID = "7100"
OUTSIDE_ID = "7200"
LIBRARY_ID = "library-scope-recovery"


class _CredentialSource:
    def load(self):
        return "unused"


class _Transport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def fs_files(self, payload, *, timeout_seconds):
        del timeout_seconds
        self.calls.append(dict(payload))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def fs_info(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        raise AssertionError("unexpected fs_info call")


def _page(records):
    return {
        "state": True,
        "data": records,
        "offset": 0,
        "limit": 1,
        "count": 1,
    }


def _directory(directory_id, parent_id):
    return {
        "fc": 0,
        "cid": directory_id,
        "pid": parent_id,
        "fn": "child-directory",
        "s": "0",
    }


def _file(file_id, parent_id):
    return {
        "fc": 1,
        "fid": file_id,
        "cid": parent_id,
        "fn": "episode.mkv",
        "s": "1",
    }


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scope-recovery.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="Scope recovery library",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
    return database


def _gateway(transport, authorized_directory_ids):
    return P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=tuple(authorized_directory_ids),
        request_timeout_seconds=30,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_worker_restores_durable_child_scope_without_remote_scope_expansion(
    tmp_path,
):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="cross-worker")
    first_transport = _Transport(
        [_page([_directory(CHILD_ID, ROOT_ID)]), asyncio.CancelledError()]
    )
    first_scopes = []

    def first_factory(root_directory_id, authorized_directory_ids):
        first_scopes.append((root_directory_id, frozenset(authorized_directory_ids)))
        return _gateway(first_transport, authorized_directory_ids)

    first_worker = LibraryScanWorker(
        database.session_factory,
        service,
        first_factory,
        owner="worker-one",
    )
    assert await first_worker.run_once() is True

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
        checkpoint = await session.get(LibraryScanCheckpoint, queued.run_id)
        assert run is not None
        assert checkpoint is not None
        cursor = json.loads(checkpoint.cursor_json)
        assert cursor["visited"] == [ROOT_ID, CHILD_ID]
        assert cursor["pending"][0]["directory_id"] == CHILD_ID

    second_transport = _Transport([_page([_file("8100", CHILD_ID)])])
    second_scopes = []

    def second_factory(root_directory_id, authorized_directory_ids):
        second_scopes.append((root_directory_id, frozenset(authorized_directory_ids)))
        return _gateway(second_transport, authorized_directory_ids)

    second_worker = LibraryScanWorker(
        database.session_factory,
        service,
        second_factory,
        owner="worker-two",
    )
    assert await second_worker.run_once() is True

    summary = await service.get(LIBRARY_ID, queued.run_id)
    assert summary.state == "completed"
    assert summary.complete is True
    assert first_scopes == [(ROOT_ID, frozenset({ROOT_ID}))]
    assert second_scopes == [(ROOT_ID, frozenset({ROOT_ID, CHILD_ID}))]
    assert [call["cid"] for call in second_transport.calls] == [CHILD_ID]
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v2_durable_cursor_restores_child_scope_without_remote_scope_expansion(
    tmp_path,
):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="v2-cross-worker")
    lease = await service.claim_next(owner="worker-two")
    assert lease is not None

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, queued.run_id)
        checkpoint = await session.get(LibraryScanCheckpoint, queued.run_id)
        assert run is not None
        assert checkpoint is not None
        checkpoint.cursor_json = json.dumps(
            {
                "version": 2,
                "directory_totals": {ROOT_ID: 1},
                "expected_total": 1,
                "pending": [
                    {
                        "directory_id": CHILD_ID,
                        "parent_path": "child-directory",
                        "page": 1,
                        "page_count": None,
                        "total": None,
                        "items_seen": 0,
                    }
                ],
                "visited": [ROOT_ID, CHILD_ID],
            }
        )
        checkpoint.page = 1
        checkpoint.items_seen = 1
        run.pages_read = 1
        run.items_seen = 1
        run.expected_total = 1
        session.add(
            LibraryScanEntry(
                scan_run_id=queued.run_id,
                object_type="directory",
                object_id=CHILD_ID,
                parent_id=ROOT_ID,
                name="child-directory",
                path="child-directory",
                is_directory=True,
                size_bytes=0,
            )
        )
        await session.commit()

    transport = _Transport([_page([_file("8100", CHILD_ID)])])
    scope = await service.readonly_directory_scope(lease)
    gateway = _gateway(transport, scope)
    page = await gateway.list_directory(CHILD_ID, page=1, page_size=1)

    assert scope == frozenset({ROOT_ID, CHILD_ID})
    assert page.items[0].parent_id == CHILD_ID
    assert [call["cid"] for call in transport.calls] == [CHILD_ID]
    await service.release(lease, requeue=True)
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_durable_cursor_outside_root_is_rejected_before_gateway_creation(tmp_path):
    database = await _database(tmp_path)
    service = LibraryScanOperationService(database.session_factory)
    queued = await service.enqueue(LIBRARY_ID, idempotency_key="outside-cursor")
    async with database.session_factory() as session:
        checkpoint = await session.get(LibraryScanCheckpoint, queued.run_id)
        assert checkpoint is not None
        checkpoint.cursor_json = json.dumps(
            {
                "version": 1,
                "pending": [
                    {
                        "directory_id": OUTSIDE_ID,
                        "parent_path": "outside",
                        "page": 1,
                        "page_count": None,
                        "total": None,
                    }
                ],
                "visited": [ROOT_ID, OUTSIDE_ID],
            }
        )
        session.add(
            LibraryScanEntry(
                scan_run_id=queued.run_id,
                object_type="directory",
                object_id=OUTSIDE_ID,
                parent_id="9999",
                name="outside-directory",
                path="outside",
                is_directory=True,
                size_bytes=0,
            )
        )
        await session.commit()

    factory_calls = []

    def gateway_factory(root_directory_id, authorized_directory_ids):
        factory_calls.append((root_directory_id, frozenset(authorized_directory_ids)))
        raise AssertionError("out-of-scope cursor must not create a gateway")

    worker = LibraryScanWorker(
        database.session_factory,
        service,
        gateway_factory,
        owner="worker-two",
    )
    assert await worker.run_once() is True

    summary = await service.get(LIBRARY_ID, queued.run_id)
    assert summary.state == "failed"
    assert summary.error_code == "library_scope_unverified"
    assert factory_calls == []
    await database.engine.dispose()
