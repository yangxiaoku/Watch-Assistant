from pathlib import Path

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.p115_delete import DeleteStatus, P115DeleteService


class _CookieProvider:
    def load(self):
        return "UID=1; CID=2; KID=3; SEID=4"


class _Client:
    def __init__(self):
        self.deleted = False
        self.cleaned = False
        self.recycle_records = [
            {
                "id": "899",
                "cid": "7000",
                "file_name": "gone.mkv",
                "file_size": "100",
            }
        ]

    def fs_files(self, payload, **kwargs):
        if self.deleted:
            return {"state": True, "data": [], "offset": 0, "limit": 1, "count": 0}
        return {
            "state": True,
            "data": [{"fc": 1, "fid": "100", "cid": "7000", "n": "gone.mkv"}],
            "offset": 0,
            "limit": 1,
            "count": 1,
        }

    def fs_delete(self, payload, **kwargs):
        self.deleted = True
        self.recycle_records.append(
            {
                "id": "900",
                "cid": "7000",
                "file_name": "gone.mkv",
                "file_size": "100",
            }
        )
        return {"state": True}

    def recyclebin_list(self, payload, **kwargs):
        records = [] if self.cleaned else self.recycle_records
        if self.cleaned:
            return {"state": True, "count": 0}
        return {
            "state": True,
            "data": records,
        }

    def recyclebin_clean(self, payload, **kwargs):
        assert payload["tid"] == "900"
        self.cleaned = True
        return {"state": True}


async def test_delete_requires_local_snapshot_and_verifies_postcondition(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delete.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-delete",
                name="DELETE",
                root_directory_id="7000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-delete",
                library_id="library-delete",
                root_directory_id="7000",
                idempotency_key="delete-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-delete",
                object_type="file",
                object_id="100",
                parent_id="7000",
                name="gone.mkv",
                path="gone.mkv",
                is_directory=False,
                size_bytes=100,
            )
        )
        await session.commit()
    client = _Client()

    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    service = P115DeleteService(
        database.session_factory,
        _CookieProvider(),
        client_factory=lambda _cookie: client,
        call_executor=call_executor,
    )
    result = await service.delete(
        "library-delete",
        "100",
        expected_name="gone.mkv",
        confirmed=True,
    )
    assert result.status is DeleteStatus.SUCCESS
    assert client.deleted is True
    assert client.cleaned is True
    await database.engine.dispose()


class _PagedRecycleClient:
    """回收站返回 250 条(3 页),验证 list_entries 分页遍历。"""

    def __init__(self) -> None:
        self.records = [
            {"id": str(1000 + i), "cid": "7000", "file_name": f"gone-{i}.mkv", "file_size": str(100 + i)}
            for i in range(250)
        ]

    def recyclebin_list(self, payload, **kwargs):
        offset = int(payload.get("offset", 0))
        limit = int(payload.get("limit", 100))
        page = self.records[offset : offset + limit]
        return {"state": True, "data": page, "offset": offset, "limit": limit, "count": len(self.records)}


async def test_recycle_bin_listing_paginates_beyond_first_page(tmp_path: Path):
    """修复前只读前 100 条:超出窗口的新回收记录永远找不到,永久删除恒 UNCERTAIN。"""
    from watch_assistant.adapters.p115_permanent_delete_transport import (
        P115PermanentDeleteTransport,
    )

    client = _PagedRecycleClient()
    calls: list[int] = []

    async def call_executor(method, payload, *, timeout_seconds):
        calls.append(int(payload.get("offset", 0)))
        return method(payload, async_=False)

    transport = P115PermanentDeleteTransport(
        client=client,
        call_executor=call_executor,
    )
    entries = await transport.list_entries(timeout_seconds=5.0)

    assert entries is not None
    assert len(entries) == 250
    assert calls == [0, 100, 200]  # 三页
    assert entries[-1].name == "gone-249.mkv"

    # find_new_entries 能命中窗口之外的记录(修复前恒为空)
    found = await transport.find_new_entries(
        set(),
        parent_id="7000",
        name="gone-249.mkv",
        size_bytes=349,
        timeout_seconds=5.0,
    )
    assert found and found[0].recycle_id == "1249"
