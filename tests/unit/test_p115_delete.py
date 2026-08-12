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


class _CollisionRecycleClient:
    """第 1 帧只有碰撞条目,第 2 帧起真实条目也出现(同目录+同名+同大小)。"""

    def __init__(self):
        self.poll_count = 0
        self._collision = [
            {"id": "collision", "cid": "7000", "file_name": "same.mkv", "file_size": "100"}
        ]
        self._real = [
            {"id": "real", "cid": "7000", "file_name": "same.mkv", "file_size": "100"}
        ]

    def recyclebin_list(self, payload, **kwargs):
        self.poll_count += 1
        records = self._collision + (self._real if self.poll_count >= 2 else [])
        offset = int(payload.get("offset", 0))
        limit = int(payload.get("limit", 100))
        return {
            "state": True,
            "data": records[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "count": len(records),
        }


async def test_find_new_entries_rejects_ambiguous_collision():
    """H1 身份碰撞:并发动作使回收站出现同身份候选时,不得任选一个执行
    不可逆清理;出现 ≥2 候选即返回 None(UNCERTAIN,fail-closed)。"""
    from watch_assistant.adapters.p115_permanent_delete_transport import (
        P115PermanentDeleteTransport,
    )

    client = _CollisionRecycleClient()

    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    transport = P115PermanentDeleteTransport(
        client=client,
        call_executor=call_executor,
    )
    found = await transport.find_new_entries(
        set(),
        parent_id="7000",
        name="same.mkv",
        size_bytes=100,
        timeout_seconds=5.0,
    )
    assert found is None


async def _delete_service_factory(database, client):
    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    return P115DeleteService(
        database.session_factory,
        _CookieProvider(),
        client_factory=lambda _cookie: client,
        call_executor=call_executor,
    )


async def test_delete_gate_denies_when_gate_raises(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delete-gate.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-gate",
                name="GATE",
                root_directory_id="7000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-gate",
                library_id="library-gate",
                root_directory_id="7000",
                idempotency_key="gate-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-gate",
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
    service = await _delete_service_factory(database, client)

    def denying_gate():
        raise RuntimeError("permanent_delete_disabled")

    result = await service.delete(
        "library-gate",
        "100",
        expected_name="gone.mkv",
        confirmed=True,
        gate=denying_gate,
    )
    assert result.status is DeleteStatus.FAILED
    assert result.error_code == "delete_gate_denied"
    assert client.deleted is False
    await database.engine.dispose()


async def test_delete_gate_is_optional_and_success_path_unchanged(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delete-optional.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-optional",
                name="OPTIONAL",
                root_directory_id="7000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-optional",
                library_id="library-optional",
                root_directory_id="7000",
                idempotency_key="optional-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-optional",
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
    service = await _delete_service_factory(database, client)

    # 未传 gate(旧调用方式)行为不变;传通过的门禁也不拦截。
    result = await service.delete(
        "library-optional",
        "100",
        expected_name="gone.mkv",
        confirmed=True,
    )
    assert result.status is DeleteStatus.SUCCESS
    assert client.deleted is True

    client2 = _Client()
    service2 = await _delete_service_factory(database, client2)
    result = await service2.delete(
        "library-optional",
        "100",
        expected_name="gone.mkv",
        confirmed=True,
        gate=lambda: None,
    )
    assert result.status is DeleteStatus.SUCCESS
    assert client2.deleted is True
    await database.engine.dispose()


class _SizeMissingRecycleClient:
    """回收站恒返回一条同目录+同名记录,可配置是否携带 size。"""

    def __init__(self, *, with_size: bool):
        record = {"id": "1", "cid": "7000", "file_name": "same.mkv"}
        if with_size:
            record["file_size"] = "100"
        self.records = [record]

    def recyclebin_list(self, payload, **kwargs):
        return {"state": True, "data": self.records}


async def test_find_new_entries_target_size_missing_fails_closed():
    """目标 size_bytes 缺失时,不得退化为按 (parent_id, name) 匹配:否则
    并发动作删除的同目录同名文件会被误判为目标,对不可逆清理造成身份碰撞。"""
    from watch_assistant.adapters.p115_permanent_delete_transport import (
        P115PermanentDeleteTransport,
    )

    client = _SizeMissingRecycleClient(with_size=True)

    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    transport = P115PermanentDeleteTransport(
        client=client,
        call_executor=call_executor,
    )
    found = await transport.find_new_entries(
        set(),
        parent_id="7000",
        name="same.mkv",
        size_bytes=None,
        timeout_seconds=5.0,
    )
    assert found is None


async def test_find_new_entries_skips_entry_without_size_fails_closed():
    """回收站条目 size 缺失时,不得匹配:身份未确认的条目不应被清理。"""
    from watch_assistant.adapters.p115_permanent_delete_transport import (
        P115PermanentDeleteTransport,
    )

    client = _SizeMissingRecycleClient(with_size=False)

    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    transport = P115PermanentDeleteTransport(
        client=client,
        call_executor=call_executor,
    )
    found = await transport.find_new_entries(
        set(),
        parent_id="7000",
        name="same.mkv",
        size_bytes=100,
        timeout_seconds=5.0,
    )
    assert found is None


async def _delete_fixture(tmp_path: Path, *, library_enabled=True, scope_verified=True, name="gone.mkv"):
    """构造一个可删除的库/扫描/条目,返回 (database, service)。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delete-fc.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-fc",
                name="FC",
                root_directory_id="7000",
                scope_verified=scope_verified,
                enabled=library_enabled,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-fc",
                library_id="library-fc",
                root_directory_id="7000",
                idempotency_key="fc-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-fc",
                object_type="file",
                object_id="100",
                parent_id="7000",
                name=name,
                path=name,
                is_directory=False,
                size_bytes=100,
            )
        )
        await session.commit()

    async def call_executor(method, payload, *, timeout_seconds):
        return method(payload, async_=False)

    return database, P115DeleteService(
        database.session_factory,
        _CookieProvider(),
        client_factory=lambda _cookie: _Client(),
        call_executor=call_executor,
    )


class _EmptyCookieProvider:
    def load(self):
        return None


async def test_delete_confirmation_required_fails_closed(tmp_path: Path):
    """未确认的永久删除必须 fail-closed,不得执行任何写操作。"""
    database, service = await _delete_fixture(tmp_path)
    try:
        result = await service.delete(
            "library-fc", "100", expected_name="gone.mkv", confirmed=False
        )
        assert result.status is DeleteStatus.FAILED
        assert result.error_code == "confirmation_required"
    finally:
        await database.engine.dispose()


async def test_delete_precondition_changed_fails_closed_when_name_mismatch(tmp_path: Path):
    """条目名称与预期不符(远端已变化)必须 fail-closed。"""
    database, service = await _delete_fixture(tmp_path, name="renamed.mkv")
    try:
        result = await service.delete(
            "library-fc", "100", expected_name="gone.mkv", confirmed=True
        )
        assert result.status is DeleteStatus.FAILED
        assert result.error_code == "delete_precondition_changed"
    finally:
        await database.engine.dispose()


async def test_delete_scope_unverified_fails_closed(tmp_path: Path):
    """库未 scope_verified 时必须 fail-closed,不得执行删除。"""
    database, service = await _delete_fixture(tmp_path, scope_verified=False)
    try:
        result = await service.delete(
            "library-fc", "100", expected_name="gone.mkv", confirmed=True
        )
        assert result.status is DeleteStatus.FAILED
        assert result.error_code == "delete_precondition_changed"
    finally:
        await database.engine.dispose()


async def test_delete_credential_unavailable_fails_closed(tmp_path: Path):
    """115 cookie 缺失时必须 fail-closed,不得执行删除。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delete-cred.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                MediaLibrary(
                    id="library-cred",
                    name="CRED",
                    root_directory_id="7000",
                    scope_verified=True,
                    enabled=True,
                    revision=1,
                )
            )
            await session.flush()
            session.add(
                LibraryScanRun(
                    id="scan-cred",
                    library_id="library-cred",
                    root_directory_id="7000",
                    idempotency_key="cred-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=1,
                )
            )
            await session.flush()
            session.add(
                LibraryScanEntry(
                    scan_run_id="scan-cred",
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
        service = P115DeleteService(
            database.session_factory,
            _EmptyCookieProvider(),
            client_factory=lambda _cookie: _Client(),
        )
        result = await service.delete(
            "library-cred", "100", expected_name="gone.mkv", confirmed=True
        )
        assert result.status is DeleteStatus.FAILED
        assert result.error_code == "credential_unavailable"
    finally:
        await database.engine.dispose()
