import json
import time

import pytest

import scripts.p115_c03_fixture_recover as recover
from watch_assistant.adapters.p115_c03_fixture_probe import C03RemoteEntry
from watch_assistant.adapters.p115_c03_live_transport import (
    MAX_RECOVERY_FS_FILES_PAGE_CALLS,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import (
    OrganizationTransportOperation,
    OrganizationTransportResult,
    OrganizationTransportStatus,
)


class _FakeP115Client:
    def __init__(self, *, fs_files=(), history=None):
        self._fs_files = list(fs_files)
        self._history = history
        self.calls = []

    def fs_files(self, payload, **kwargs):
        self.calls.append(("fs_files", dict(payload)))
        if not self._fs_files:
            raise AssertionError("unexpected fs_files call")
        return self._fs_files.pop(0)

    def fs_history_rename_list_app(self, payload, **kwargs):
        self.calls.append(("fs_history_rename_list_app", dict(payload)))
        return self._history

    def fs_mkdir(self, payload, **kwargs):
        self.calls.append(("fs_mkdir", dict(payload)))
        raise AssertionError("unexpected write")

    def fs_move(self, payload, **kwargs):
        self.calls.append(("fs_move", dict(payload)))
        raise AssertionError("unexpected write")

    def fs_rename(self, payload, **kwargs):
        self.calls.append(("fs_rename", dict(payload)))
        raise AssertionError("unexpected write")

    def fs_delete(self, payload, **kwargs):
        self.calls.append(("fs_delete", dict(payload)))
        raise AssertionError("unexpected write")


def _executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


def _page(record, *, offset, count):
    return {
        "state": True,
        "data": [record] if record is not None else [],
        "offset": offset,
        "limit": 1,
        "count": count,
    }


def _directory(file_id, parent_id, name):
    return {"fc": 0, "cid": file_id, "pid": parent_id, "n": name}


def _file(file_id, parent_id, name):
    return {"fc": 1, "fid": file_id, "cid": parent_id, "n": name}


def _enable_writes(monkeypatch):
    for name in recover._GATES:
        monkeypatch.setenv(name, "1")


@pytest.mark.asyncio
async def test_recovery_preview_reads_more_than_four_pages_and_exact_c03_scope(
    monkeypatch,
):
    pages = [
        _page(_directory("10", "7", "wa-c03-root-fixture"), offset=0, count=6),
        _page(_directory("11", "7", "wa-org-probe-dir-ignored"), offset=1, count=6),
        _page(_file("12", "7", "unrelated.txt"), offset=2, count=6),
        _page(_directory("13", "7", "ordinary-directory"), offset=3, count=6),
        _page(_file("14", "7", "another.txt"), offset=4, count=6),
        _page(_directory("15", "7", "other-root"), offset=5, count=6),
    ]
    client = _FakeP115Client(fs_files=pages)
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)

    result = await recover._run(
        client,
        "7",
        recycle=False,
        restore_organization=False,
        authorization_path=None,
        managed_parent_ids=("7",),
    )

    assert result == {
        "status": "preview",
        "candidate_count": 1,
        "inventory_complete": True,
        "recycle_started": False,
    }
    assert [payload["offset"] for _, payload in client.calls] == list(range(6))
    assert all(payload["limit"] == 1 for _, payload in client.calls)
    assert recover._managed_roots(
        (C03RemoteEntry("99", "8", "wa-c03-root-foreign"),), "7"
    ) == ()


@pytest.mark.asyncio
async def test_recovery_requires_managed_scope_before_listing(monkeypatch):
    client = _FakeP115Client()
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)

    result = await recover._run(
        client,
        "7",
        recycle=False,
        restore_organization=False,
        authorization_path=None,
    )

    assert result == {"status": "blocked", "error_code": "recovery_scope_unverified"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_recovery_preview_fails_closed_at_its_page_bound(monkeypatch):
    count = MAX_RECOVERY_FS_FILES_PAGE_CALLS + 1
    pages = [
        _page(_directory(str(index + 1), "7", f"ordinary-{index}"), offset=index, count=count)
        for index in range(count)
    ]
    client = _FakeP115Client(fs_files=pages)
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)

    result = await recover._run(
        client,
        "7",
        recycle=False,
        restore_organization=False,
        authorization_path=None,
        managed_parent_ids=("7",),
    )

    assert result == {"status": "blocked", "error_code": "inventory_incomplete"}
    assert len(client.calls) == MAX_RECOVERY_FS_FILES_PAGE_CALLS


@pytest.mark.asyncio
async def test_recovery_restore_blocks_when_c03_candidates_are_not_unique(
    monkeypatch, tmp_path
):
    pages = [
        _page(_directory("10", "7", "wa-c03-root-one"), offset=0, count=5),
        _page(_directory("11", "7", "wa-c03-root-two"), offset=1, count=5),
        _page(_file("12", "7", "unrelated.txt"), offset=2, count=5),
        _page(_directory("13", "7", "ordinary"), offset=3, count=5),
        _page(_file("14", "7", "another.txt"), offset=4, count=5),
    ]
    client = _FakeP115Client(fs_files=pages)
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)

    result = await recover._run(
        client,
        "7",
        recycle=False,
        restore_organization=True,
        authorization_path=tmp_path / "authorization.json",
        managed_parent_ids=("7",),
    )

    assert result == {"status": "blocked", "error_code": "recovery_scope_unverified"}
    assert [name for name, _ in client.calls] == ["fs_files"] * 5


@pytest.mark.asyncio
async def test_recovery_write_requires_one_shot_authorization_before_recycle(
    monkeypatch,
):
    _enable_writes(monkeypatch)
    client = _FakeP115Client(
        fs_files=[_page(_directory("10", "7", "wa-c03-root-one"), offset=0, count=1)]
    )
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)

    result = await recover._run(
        client,
        "7",
        recycle=True,
        restore_organization=False,
        authorization_path=None,
        managed_parent_ids=("7",),
    )

    assert result == {"status": "blocked", "error_code": "authorization_required"}
    assert [name for name, _ in client.calls] == ["fs_files"]


@pytest.mark.asyncio
async def test_recovery_stops_after_uncertain_move_and_does_not_rename(
    monkeypatch, tmp_path
):
    _enable_writes(monkeypatch)
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "version": 1,
                "parent_id": "7",
                "expires_at": time.time() + 60,
                "nonce": "offline",
            }
        ),
        encoding="ascii",
    )
    client = _FakeP115Client(
        fs_files=[_page(_file("20", "10", "temporary.mkv"), offset=0, count=1)],
        history={
            "state": True,
            "data": [{"file_id": "20", "file_old_name": "original.mkv"}],
        },
    )
    fake_transport = _UncertainOrganizationTransport()
    monkeypatch.setattr(recover, "_p115client_timeout_executor", _executor)
    monkeypatch.setattr(
        recover,
        "create_live_p115_organization_transport",
        lambda **kwargs: fake_transport,
    )

    result = await recover._restore_organization(
        client,
        "7",
        C03RemoteEntry("10", "7", "wa-c03-root-one"),
        authorization,
    )

    assert result == {
        "status": "uncertain",
        "error_code": "recovery_move_unconfirmed",
    }
    assert fake_transport.calls == ["read_object", "read_target", "move", "read_object"]
    assert not any(name in {"fs_move", "fs_rename", "fs_delete"} for name, _ in client.calls)


class _UncertainOrganizationTransport:
    def __init__(self):
        self.calls = []

    async def read_object(self, object_id):
        self.calls.append("read_object")
        return RemoteObjectState(object_id, "10", "temporary.mkv")

    async def read_target(self, parent_id, name):
        self.calls.append("read_target")

    async def move(self, object_id, parent_id):
        self.calls.append("move")
        return OrganizationTransportResult(
            OrganizationTransportOperation.MOVE,
            OrganizationTransportStatus.UNCERTAIN,
            "outcome_unknown",
        )

    async def rename(self, object_id, name):
        self.calls.append("rename")
        raise AssertionError("rename must not follow uncertain move")
