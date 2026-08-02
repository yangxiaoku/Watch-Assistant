import pytest

from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupError,
    EmptyDirectoryCleanupStatus,
    LiveP115EmptyDirectoryCleaner,
)


class _FakeClient:
    def __init__(self, responses):
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls = []

    def _response(self, name, payload, **kwargs):
        self.calls.append((name, dict(payload), dict(kwargs)))
        return self.responses[name].pop(0)

    def fs_delete(self, payload, **kwargs):
        return self._response("fs_delete", payload, **kwargs)

    def fs_files(self, payload, **kwargs):
        return self._response("fs_files", payload, **kwargs)

    def fs_files_app(self, payload, **kwargs):
        return self._response("fs_files_app", payload, **kwargs)

    def fs_info(self, payload, **kwargs):
        return {"state": True, "data": {"fc": 0, "pid": payload["cid"]}}


def _page(records, *, count):
    return {
        "state": True,
        "data": records,
        "offset": 0,
        "limit": 1,
        "count": count,
    }


def _directory(directory_id, parent_id, name):
    return {
        "fc": 0,
        "cid": directory_id,
        "pid": parent_id,
        "n": name,
    }


async def _executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


@pytest.mark.asyncio
async def test_empty_directory_cleanup_requires_full_verified_scope_and_postcondition():
    client = _FakeClient(
        {
            "fs_delete": [{"state": True}],
            "fs_files": [
                _page([_directory("7000", "8000", "old-show")], count=1),
                _page([], count=0),
                _page([], count=0),
            ],
        }
    )
    cleaner = LiveP115EmptyDirectoryCleaner(
        client=client,
        call_executor=_executor,
        managed_directory_ids=("1000", "8000", "7000", "9000"),
        system_created_directory_ids=("7000",),
        scope_confirmed=True,
    )

    result = await cleaner.cleanup("7000", "8000", "old-show")

    assert result is EmptyDirectoryCleanupStatus.SUCCESS
    assert [call[0] for call in client.calls] == [
        "fs_files",
        "fs_files",
        "fs_delete",
        "fs_files",
    ]
    assert client.calls[2][1] == {"fid": "7000"}


@pytest.mark.asyncio
async def test_empty_directory_cleanup_rejects_scope_not_in_complete_scan():
    client = _FakeClient({"fs_delete": [], "fs_files": []})
    cleaner = LiveP115EmptyDirectoryCleaner(
        client=client,
        call_executor=_executor,
        managed_directory_ids=("1000", "8000"),
        system_created_directory_ids=("8000",),
        scope_confirmed=True,
    )

    with pytest.raises(EmptyDirectoryCleanupError, match="cleanup_scope_unverified"):
        await cleaner.cleanup("7000", "8000", "old-show")
    assert client.calls == []


@pytest.mark.asyncio
async def test_empty_directory_cleanup_requires_system_created_evidence_before_remote_reads():
    client = _FakeClient({"fs_delete": [], "fs_files": []})
    cleaner = LiveP115EmptyDirectoryCleaner(
        client=client,
        call_executor=_executor,
        managed_directory_ids=("1000", "8000", "7000"),
        system_created_directory_ids=(),
        scope_confirmed=True,
    )

    with pytest.raises(EmptyDirectoryCleanupError, match="cleanup_scope_unverified"):
        await cleaner.cleanup("7000", "8000", "old-show")
    assert client.calls == []
