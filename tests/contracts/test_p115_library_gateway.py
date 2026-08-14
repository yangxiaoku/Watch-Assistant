import asyncio

import pytest

from watch_assistant.adapters.p115_library import ScanState, scan_directory
from watch_assistant.adapters.p115_library_gateway import (
    VERIFIED_BATCH_PAGE_SIZE,
    VERIFIED_PAGE_SIZE,
    VIRTUAL_ROOT_PAGE_SIZE,
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)


class _CredentialSource:
    def __init__(self, value="SYNTHETIC_COOKIE_SECRET") -> None:
        self._value = value
        self.calls = 0

    def load(self):
        self.calls += 1
        return self._value

    def __repr__(self) -> str:
        return f"_CredentialSource(call_count={self.calls})"


class _FsFilesClient:
    """双索引 mock:show_dir=1 弹文件夹索引,show_dir=0 弹文件索引,fs_info 判型。

    真实 115(2026-08-14 实测):legacy 文件夹索引记录没有 fid 且 fc 恒 0,必须用
    fs_info 的 folder_count>0 判型;文件索引记录带 fid,直接按文件解析。
    """

    def __init__(
        self, responses, *, file_indexes=(), directories=(), content_files=()
    ) -> None:
        self._responses = list(responses)
        self._file_indexes = list(file_indexes)
        self._directories = set(directories)
        self._content_files = set(content_files)
        self.calls = []
        self.detail_calls = []

    def fs_files(self, payload):
        self.calls.append(dict(payload))
        if payload.get("show_dir") == 0:
            if not self._file_indexes:
                raise AssertionError("unexpected file-index request")
            return self._file_indexes.pop(0)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def fs_info(self, payload):
        self.detail_calls.append(dict(payload))
        value = str(payload.get("cid") or payload.get("fid") or "")
        is_directory = value in self._directories
        has_content = value in self._content_files
        return {
            "state": True,
            "folder_count": 1 if is_directory else 0,
            "play_long": 9947 if has_content else 0,
            "size": "1.32GB" if has_content else "0B",
            "file_name": "classified-entry",
            "paths": [],
        }

    def __repr__(self) -> str:
        return f"_FsFilesClient(call_count={len(self.calls)})"


def _file(fid="101", parent="7", name="secret-file-name.mkv"):
    """app 文件夹索引形态:文件与目录同形(fid+fc=\"0\"),需 fs_info 判型。"""
    return {
        "fc": "0",
        "fid": fid,
        "pid": parent,
        "fn": name,
        "s": "123",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _file_index_record(fid="101", cid="101", name="secret-file-name.mkv"):
    """文件索引形态:带 fid/play_long;webapi id 在 legacy 记录的 cid、app 记录的
    pid(父级即被列出的目录,由 gateway 注入)。"""
    return {
        "fc": 1,
        "fid": fid,
        "cid": cid,
        "n": name,
        "s": "123",
        "play_long": 9947,
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _folder_entry(cid="8", parent="7", name="secret-directory"):
    """legacy 文件夹索引形态:文件与目录同形(fc=0、无 fid),需 fs_info 判型。"""
    return {
        "fc": 0,
        "cid": cid,
        "pid": parent,
        "fn": name,
        "s": "0",
    }


def _page(records, *, offset=0, count=None, limit=1, state=True):
    return {
        "state": state,
        "data": records,
        "offset": offset,
        "limit": limit,
        "count": len(records) if count is None else count,
    }


def _folder_payload(cid, *, limit=VERIFIED_PAGE_SIZE, offset=0):
    return {
        "cid": cid,
        "limit": limit,
        "offset": offset,
        "record_open_time": 0,
        "show_dir": 1,
    }


def _file_index_payload(cid, *, limit=VERIFIED_PAGE_SIZE, offset=0):
    return {
        "cid": cid,
        "limit": limit,
        "offset": offset,
        "record_open_time": 0,
        "show_dir": 0,
    }


def _gateway(
    client,
    credential_source=None,
    *,
    authorized_directory_ids=("7",),
    authorized_file_ids=(),
    allow_virtual_root=False,
):
    source = credential_source or _CredentialSource()
    factory_calls = []

    class Transport:
        def __init__(self):
            self.timeouts = []

        async def fs_files_app(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            method = getattr(client, "fs_files_app", None)
            return method(payload) if callable(method) else client.fs_files(payload)

        async def fs_files(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return client.fs_files(payload)

        async def fs_info_app(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            method = getattr(client, "fs_info_app", None)
            if callable(method):
                return method(payload)
            method = getattr(client, "fs_info", None)
            if method is None:
                raise AssertionError("unexpected fs_info_app call")
            return method(payload)

        async def fs_info(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            method = getattr(client, "fs_info", None)
            if method is None:
                raise AssertionError("unexpected fs_info call")
            return method(payload)

    transport = Transport()

    def factory(credential):
        factory_calls.append(credential)
        return transport

    return (
        P115ReadOnlyDirectoryGateway(
            source,
            factory,
            authorized_directory_ids=authorized_directory_ids,
            authorized_file_ids=authorized_file_ids,
            allow_virtual_root=allow_virtual_root,
        ),
        source,
        factory_calls,
    )


@pytest.mark.asyncio
async def test_verified_fs_files_page_maps_pickcode_without_path_or_repr_leaks():
    client = _FsFilesClient(
        (_page([_file()]),), file_indexes=(_page([], count=0),)
    )
    gateway, source, factory_calls = _gateway(client)

    result = await gateway.list_directory("7")

    assert result.scan_complete is True
    assert result.terminal is True
    assert result.has_more is False
    assert result.items[0].pickcode == "SYNTHETIC_PICKCODE_SECRET"
    assert result.items[0].path is None
    assert client.calls == [
        _folder_payload("7"),
        _file_index_payload("7"),
    ]
    assert client.detail_calls == [{"fid": "101"}]
    assert source.calls == 1
    assert len(factory_calls) == 1
    rendered = repr(gateway) + repr(result) + repr(result.items[0]) + repr(client)
    assert "SYNTHETIC_COOKIE_SECRET" not in rendered
    assert "SYNTHETIC_PICKCODE_SECRET" not in rendered
    assert "secret-file-name.mkv" not in rendered
    assert "101" not in rendered


@pytest.mark.asyncio
async def test_batch_page_size_reads_full_page_in_one_call():
    records = [_file(fid=str(101 + i), name=f"file-{i}.mkv") for i in range(3)]
    client = _FsFilesClient(
        (_page(records, offset=0, count=3, limit=VERIFIED_BATCH_PAGE_SIZE),),
        file_indexes=(_page([], count=0, limit=VERIFIED_BATCH_PAGE_SIZE),),
    )
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory("7", page_size=VERIFIED_BATCH_PAGE_SIZE)

    assert client.calls == [
        _folder_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE),
        _file_index_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE),
    ]
    assert len(result.items) == 3
    assert result.terminal is True
    assert result.has_more is False
    assert result.next_page is None
    assert result.total == 3


@pytest.mark.asyncio
async def test_batch_page_size_paginates_with_batch_offsets():
    client = _FsFilesClient(
        (_page([_folder_entry(cid="8")], count=1, limit=VERIFIED_BATCH_PAGE_SIZE),),
        file_indexes=(
            _page(
                [_file_index_record(fid="201", cid="201")],
                offset=0,
                count=51,
                limit=VERIFIED_BATCH_PAGE_SIZE,
            ),
            _page(
                [_file_index_record(fid="202", cid="202")],
                offset=50,
                count=51,
                limit=VERIFIED_BATCH_PAGE_SIZE,
            ),
        ),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    page1 = await gateway.list_directory("7", page=1, page_size=VERIFIED_BATCH_PAGE_SIZE)
    page2 = await gateway.list_directory("7", page=2, page_size=VERIFIED_BATCH_PAGE_SIZE)

    assert page1.has_more is True
    assert page1.next_page == 2
    assert page2.terminal is True
    assert page2.has_more is False
    # 页 1 的文件夹索引(1 条) + 文件索引 + 页 2 的文件索引(文件夹索引已耗尽跳过)。
    assert [call["offset"] for call in client.calls] == [0, 0, 50]
    assert page1.total == 52
    assert page2.total == 52


@pytest.mark.asyncio
async def test_verified_app_listing_compact_pc_maps_to_pickcode():
    record = _file()
    record.pop("pick_code")
    record["pc"] = "SYNTHETIC_PICKCODE_SECRET"
    client = _FsFilesClient(
        (_page([record]),), file_indexes=(_page([], count=0),)
    )
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory("7")

    assert result.items[0].pickcode == "SYNTHETIC_PICKCODE_SECRET"
    assert "SYNTHETIC_PICKCODE_SECRET" not in repr(result.items[0])


@pytest.mark.asyncio
async def test_consecutive_offset_count_pages_complete_only_at_verified_terminal_page():
    client = _FsFilesClient(
        (_page([_folder_entry(cid="8")], count=1),),
        file_indexes=(
            _page([_file_index_record(fid="101", cid="101", name="one.mkv")], count=2),
            _page(
                [_file_index_record(fid="102", cid="102", name="two.mkv")],
                offset=1,
                count=2,
            ),
        ),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    scan = await scan_directory(gateway, "7", page_size=VERIFIED_PAGE_SIZE)

    assert scan.state is ScanState.COMPLETE
    assert scan.scan_complete is True
    assert scan.pages_read == 2
    assert [call["offset"] for call in client.calls] == [0, 0, 1]
    assert {entry.file_id or entry.directory_id for entry in scan.items} == {
        "8",
        "101",
        "102",
    }


@pytest.mark.asyncio
async def test_observed_child_directory_can_be_scanned_without_expanding_scope():
    client = _FsFilesClient(
        (
            _page([_folder_entry()], count=1),
            _page([_file("102", parent="8")], count=1),
        ),
        file_indexes=(_page([], count=0), _page([], count=0)),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    child = await gateway.list_directory("8")

    assert child.items[0].file_id == "102"
    # 目录 7:文件夹索引 + 文件索引 + fs_info 判型;目录 8:文件夹索引 + 文件索引。
    assert [call["cid"] for call in client.calls] == ["7", "7", "8", "8"]
    assert client.detail_calls == [{"cid": "8"}, {"fid": "102"}]


@pytest.mark.asyncio
async def test_mismatched_parent_does_not_expand_observed_directory_scope():
    client = _FsFilesClient(
        (_page([_folder_entry(cid="8", parent="99")]),),
        file_indexes=(_page([], count=0),),
        directories=("8",),
    )
    gateway, source, factory_calls = _gateway(client)

    with pytest.raises(P115ReadOnlyGatewayError, match="entry_scope_unverified"):
        await gateway.list_directory("7")
    with pytest.raises(P115ReadOnlyGatewayError, match="scope_unverified"):
        await gateway.list_directory("8")

    assert client.calls == [
        _folder_payload("7"),
        _file_index_payload("7"),
    ]
    assert source.calls == 1
    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_restored_child_allowlist_is_usable_by_a_new_gateway_instance():
    client = _FsFilesClient(
        (_page([_file("102", parent="8")]),),
        file_indexes=(_page([], count=0),),
    )
    gateway, source, factory_calls = _gateway(
        client, authorized_directory_ids=("7", "8")
    )

    result = await gateway.list_directory("8")

    assert result.items[0].file_id == "102"
    assert client.calls[0]["cid"] == "8"
    assert source.calls == 1
    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_missing_or_inconsistent_pagination_signals_fail_closed():
    without_count = _page([_file()])
    del without_count["count"]
    missing_count, _, _ = _gateway(_FsFilesClient((without_count,)))
    with pytest.raises(P115ReadOnlyGatewayError, match="pagination_unverified"):
        await missing_count.list_directory("7")

    bad_offset, _, _ = _gateway(_FsFilesClient((_page([_file()], offset=1),)))
    with pytest.raises(P115ReadOnlyGatewayError, match="pagination_unverified"):
        await bad_offset.list_directory("7")

    bad_limit, _, _ = _gateway(_FsFilesClient((_page([_file()], limit=2),)))
    with pytest.raises(P115ReadOnlyGatewayError, match="pagination_unverified"):
        await bad_limit.list_directory("7")


@pytest.mark.asyncio
async def test_unverified_scope_or_page_size_does_not_load_credentials():
    source = _CredentialSource()
    gateway, _, factory_calls = _gateway(_FsFilesClient(()), source)

    with pytest.raises(P115ReadOnlyGatewayError, match="directory_id_unverified"):
        await gateway.list_directory("0")
    with pytest.raises(P115ReadOnlyGatewayError, match="scope_unverified"):
        await gateway.list_directory("8")
    with pytest.raises(P115ReadOnlyGatewayError, match="page_size_unverified"):
        await gateway.list_directory("7", page_size=2)
    with pytest.raises(P115ReadOnlyGatewayError, match="scope_unverified"):
        await gateway.get_file_detail("8")

    assert source.calls == 0
    assert factory_calls == []


class _DetailClient(_FsFilesClient):
    def __init__(self, response=None, pages=(), *, details=None, **kwargs):
        super().__init__(pages, **kwargs)
        self._detail_response = response
        self._details = details or {}

    def fs_info(self, payload):
        self.detail_calls.append(dict(payload))
        value = str(payload.get("cid") or payload.get("fid") or "")
        if value in self._details:
            return self._details[value]
        if self._detail_response is not None:
            return self._detail_response
        return {
            "state": True,
            "folder_count": 1 if value in self._directories else 0,
            "file_name": "classified",
            "paths": [],
        }


@pytest.mark.asyncio
async def test_fs_info_maps_verified_file_detail_without_path_or_repr_leaks():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "1",
            "folder_count": 0,
            "fid": "101",
            "cid": "7",
            "file_name": "secret-file-name.mkv",
            "ptime": "1710000000",
            "pick_code": "SYNTHETIC_PICKCODE_SECRET",
        },
        pages=(_page([_file("101")]),),
        file_indexes=(_page([], count=0),),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")

    result = await gateway.get_file_detail("101")

    # 判型 + 详情各一次 fs_info。
    assert client.detail_calls == [{"fid": "101"}, {"fid": "101"}]
    assert result.file_id == "101"
    assert result.parent_id == "7"
    assert result.pickcode == "SYNTHETIC_PICKCODE_SECRET"
    assert result.path is None
    rendered = repr(result)
    assert "secret-file-name.mkv" not in rendered
    assert "SYNTHETIC_PICKCODE_SECRET" not in rendered


@pytest.mark.asyncio
async def test_fs_info_uses_previously_observed_identity_when_response_omits_it():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "1",
            "folder_count": 0,
            "file_name": "secret-file-name.mkv",
            "ptime": "provider-local-time",
        },
        pages=(_page([_file("101")]),),
        file_indexes=(_page([], count=0),),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    result = await gateway.get_file_detail("101")

    assert result.file_id == "101"
    assert result.parent_id == "7"
    assert result.modified_at is None
    assert result.pickcode == "SYNTHETIC_PICKCODE_SECRET"


@pytest.mark.asyncio
async def test_fs_info_rejects_conflicting_identity_after_listing():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "1",
            "folder_count": 0,
            "fid": "102",
            "file_name": "secret-file-name.mkv",
        },
        pages=(_page([_file("101")]),),
        file_indexes=(_page([], count=0),),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    with pytest.raises(P115ReadOnlyGatewayError, match="detail_unverified"):
        await gateway.get_file_detail("101")


@pytest.mark.asyncio
async def test_fs_info_rejects_type_or_identity_mismatch():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "0",
            "folder_count": 1,
            "cid": "101",
            "file_name": "directory-name",
            "size": "0",
        }
    )
    gateway, _, _ = _gateway(client, authorized_file_ids=("101",))

    with pytest.raises(P115ReadOnlyGatewayError, match="detail_unverified"):
        await gateway.get_file_detail("101")


@pytest.mark.asyncio
async def test_fs_info_uses_cid_for_an_observed_directory_detail():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "0",
            "folder_count": 3,
            "cid": "8",
            "pid": "7",
            "file_name": "secret-directory",
            "size": "0",
        },
        pages=(_page([_folder_entry("8")]),),
        file_indexes=(_page([], count=0),),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    result = await gateway.get_directory_detail("8")

    # 判型 + 详情各一次 fs_info。
    assert client.detail_calls == [{"cid": "8"}, {"cid": "8"}]
    assert result.directory_id == "8"
    assert result.parent_id == "7"


@pytest.mark.asyncio
async def test_legacy_index_merge_dedupes_files_and_classifies_directories():
    """legacy 模式:文件夹索引(目录 + 未入文件索引的推送文件)与文件索引合并。

    推送文件同时出现在两个索引时按 cid 去重,避免同一文件双条目。
    """
    client = _FsFilesClient(
        (
            _page(
                [
                    _folder_entry(cid="8", parent="7", name="subdir"),
                    _folder_entry(cid="101", parent="7", name="pushed.mkv"),
                ],
                count=2,
                limit=VERIFIED_BATCH_PAGE_SIZE,
            ),
        ),
        file_indexes=(
            _page(
                [
                    _file_index_record(fid="101", cid="101", name="pushed.mkv"),
                    _file_index_record(fid="102", cid="102", name="other.mkv"),
                ],
                count=2,
                limit=VERIFIED_BATCH_PAGE_SIZE,
            ),
        ),
        directories=("8",),
        content_files=("101",),
    )
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory(
        "7", page_size=VERIFIED_BATCH_PAGE_SIZE
    )

    assert client.calls == [
        _folder_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE),
        _file_index_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE),
    ]
    assert client.detail_calls == [{"cid": "8"}, {"cid": "101"}]
    entries = {entry.file_id or entry.directory_id: entry for entry in result.items}
    assert set(entries) == {"8", "101", "102"}
    assert entries["8"].is_directory is True
    assert entries["8"].parent_id == "7"
    assert entries["101"].is_directory is False
    assert entries["101"].parent_id == "7"
    assert entries["102"].is_directory is False
    assert entries["102"].parent_id == "7"
    # 文件夹索引在首页全部返回并去重(101 已进入文件索引):
    # total = 文件夹 2 + 文件 2 - 去重 1 = 3,本页即终止。
    assert result.total == 3
    assert result.terminal is True
    assert result.has_more is False
    assert result.next_page is None


@pytest.mark.asyncio
async def test_legacy_listing_skips_exhausted_indexes_and_terminates():
    """索引越界请求会被 115 重置 offset 并返回整页,合并页必须跳过已耗尽索引。"""
    client = _FsFilesClient(
        (_page([_folder_entry(cid="8")], count=1),),
        file_indexes=(
            _page([_file_index_record(fid="201", cid="201")], offset=0, count=2),
            _page([_file_index_record(fid="202", cid="202")], offset=1, count=2),
        ),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    page1 = await gateway.list_directory("7", page=1)
    page2 = await gateway.list_directory("7", page=2)
    page3 = await gateway.list_directory("7", page=3)

    assert {entry.file_id or entry.directory_id for entry in page1.items} == {"8", "201"}
    assert [entry.file_id for entry in page2.items] == ["202"]
    assert page3.items == ()
    assert page1.terminal is False
    assert page2.terminal is True
    assert page2.has_more is False
    assert page3.terminal is True
    assert page3.has_more is False
    # 页 3 不再请求任何索引(文件夹索引 count=1、文件索引 count=2 都已耗尽)。
    assert [call["offset"] for call in client.calls] == [0, 0, 1]


@pytest.mark.asyncio
async def test_file_detail_anchors_identity_via_paths_parent_and_registers_directory():
    """observer 流程:未列表观察时,文件详情以 paths 父链(须落在授权目录内)锚定
    身份,并把父目录登记为已观察目录,使随后的 get_directory_detail 可验证身份。"""
    client = _DetailClient(
        details={
            "101": {
                "state": True,
                "file_name": "pushed.mkv",
                "folder_count": 0,
                "play_long": 0,
                "size": "0B",
                "pick_code": "SYNTHETIC_PICKCODE_SECRET",
                "paths": [
                    {"file_id": 0, "file_name": "根目录"},
                    {"file_id": "7", "file_name": "secret-directory"},
                ],
            },
            "7": {
                "state": True,
                "file_name": "secret-directory",
                "folder_count": 5,
                "play_long": 0,
                "size": "730KB",
                "pick_code": "SYNTHETIC_PICKCODE_SECRET",
                "paths": [{"file_id": 0, "file_name": "根目录"}],
            },
        }
    )
    gateway, _, _ = _gateway(
        client,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    detail = await gateway.get_file_detail("101")
    parent = await gateway.get_directory_detail("7")

    assert detail.file_id == "101"
    assert detail.parent_id == "7"
    assert detail.is_directory is False
    assert parent.directory_id == "7"
    assert parent.is_directory is True
    assert client.detail_calls == [{"fid": "101"}, {"cid": "7"}]


@pytest.mark.asyncio
async def test_file_detail_paths_parent_outside_authorized_scope_fails_closed():
    client = _DetailClient(
        details={
            "101": {
                "state": True,
                "file_name": "pushed.mkv",
                "folder_count": 0,
                "play_long": 0,
                "paths": [{"file_id": "99", "file_name": "elsewhere"}],
            }
        }
    )
    gateway, _, _ = _gateway(
        client,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    with pytest.raises(P115ReadOnlyGatewayError, match="detail_unverified"):
        await gateway.get_file_detail("101")


@pytest.mark.asyncio
async def test_gateway_passes_only_the_shared_deadline_remainder_to_transport():
    client = _FsFilesClient(
        (_page([]),), file_indexes=(_page([], count=0),)
    )
    source = _CredentialSource()
    clock_values = iter((100.0, 101.0, 102.0, 103.0, 104.0))

    class Transport:
        def __init__(self):
            self.timeouts = []

        async def fs_files_app(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return client.fs_files(payload)

        async def fs_files(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return client.fs_files(payload)

        async def fs_info_app(self, payload, *, timeout_seconds):
            raise AssertionError("unexpected fs_info_app call")

        async def fs_info(self, payload, *, timeout_seconds):
            raise AssertionError("unexpected fs_info call")

    transport = Transport()
    gateway = P115ReadOnlyDirectoryGateway(
        source,
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        request_timeout_seconds=5,
        clock=lambda: next(clock_values),
    )

    await gateway.list_directory("7")

    # 文件夹索引与文件索引各一次调用,共享同一 deadline 的剩余时间递减。
    assert transport.timeouts == [2.0, 1.0]


def test_authorized_directory_set_must_be_nonempty_stable_nonroot_ids():
    client = _FsFilesClient(())
    source = _CredentialSource()

    with pytest.raises(ValueError, match="invalid_authorized_directories"):
        P115ReadOnlyDirectoryGateway(
            source, lambda _credential: client, authorized_directory_ids=()
        )
    with pytest.raises(ValueError, match="invalid_authorized_directories"):
        P115ReadOnlyDirectoryGateway(
            source, lambda _credential: client, authorized_directory_ids=("0",)
        )


@pytest.mark.asyncio
async def test_virtual_root_can_list_children_only_when_explicitly_enabled():
    client = _FsFilesClient(
        (
            _page(
                [_folder_entry(cid="8", parent="0"), _folder_entry(cid="9", parent="0")],
                limit=VIRTUAL_ROOT_PAGE_SIZE - 2,
            ),
        ),
        file_indexes=(_page([], count=0, limit=VIRTUAL_ROOT_PAGE_SIZE),),
        directories=("8", "9"),
    )
    gateway, _, _ = _gateway(
        client,
        authorized_directory_ids=("0",),
        allow_virtual_root=True,
    )

    result = await gateway.list_directory("0")

    assert client.calls[0]["cid"] == "0"
    assert client.calls[0]["limit"] == VIRTUAL_ROOT_PAGE_SIZE
    assert client.calls[0]["offset"] == 0
    assert client.calls[1] == _file_index_payload(
        "0", limit=VIRTUAL_ROOT_PAGE_SIZE
    )
    assert len(result.items) == 2
    assert result.items[0].directory_id == "8"
    assert result.items[0].parent_id == "0"


@pytest.mark.asyncio
async def test_only_explicit_client_failures_cross_the_boundary_as_stable_codes():
    remote, _, _ = _gateway(_FsFilesClient((_page([], state=False),)))
    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await remote.list_directory("7")

    failed, _, _ = _gateway(_FsFilesClient((OSError("cookie /private/path pickcode"),)))
    with pytest.raises(P115ReadOnlyGatewayError, match="fs_files_failed") as error:
        await failed.list_directory("7")
    assert "cookie" not in str(error.value)
    assert "private" not in repr(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (("errno", 200), ("errno", "200"), ("errNo", 1), ("code", False)),
)
async def test_explicit_error_codes_fail_closed(field, value):
    response = _page([])
    response[field] = value
    gateway, _, _ = _gateway(_FsFilesClient((response,)))

    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await gateway.list_directory("7")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (("errno", 0), ("errno", False), ("code", 200), ("code", True)),
)
async def test_explicit_success_codes_follow_their_own_whitelists(field, value):
    response = _page([])
    response[field] = value
    gateway, _, _ = _gateway(
        _FsFilesClient((response,), file_indexes=(_page([], count=0),))
    )

    result = await gateway.list_directory("7")

    assert result.scan_complete is True


class _CancelledClient:
    def fs_files(self, payload):
        del payload
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_a_second_request():
    gateway, _, _ = _gateway(_CancelledClient())

    with pytest.raises(asyncio.CancelledError):
        await gateway.list_directory("7")


class _MethodNotAllowed(RuntimeError):
    status = 405


class _AppFirstTransport:
    """Transport whose app read methods fail or succeed under test control."""

    def __init__(
        self,
        *,
        app_files_error=None,
        app_info_error=None,
        app_info_response=None,
    ):
        self.calls = []
        self._app_files_error = app_files_error
        self._app_info_error = app_info_error
        self._app_info_response = app_info_response
        self._files_page = _page([_file()])
        self._file_index_page = _page([], count=0)
        self._detail = {
            "state": True,
            "file_category": "1",
            "folder_count": 0,
            "fid": "101",
            "cid": "7",
            "file_name": "secret-file-name.mkv",
        }

    def _respond(self, payload):
        return self._file_index_page if payload.get("show_dir") == 0 else self._files_page

    async def fs_files_app(self, payload, *, timeout_seconds):
        self.calls.append("fs_files_app")
        if self._app_files_error is not None:
            raise self._app_files_error
        return self._respond(payload)

    async def fs_files(self, payload, *, timeout_seconds):
        self.calls.append("fs_files")
        return self._respond(payload)

    async def fs_info_app(self, payload, *, timeout_seconds):
        self.calls.append("fs_info_app")
        if self._app_info_error is not None:
            raise self._app_info_error
        if self._app_info_response is not None:
            return self._app_info_response
        return self._detail

    async def fs_info(self, payload, *, timeout_seconds):
        self.calls.append("fs_info")
        return self._detail


@pytest.mark.asyncio
async def test_gateway_prefers_app_read_methods_when_available():
    transport = _AppFirstTransport()
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    result = await gateway.list_directory("7")
    detail = await gateway.get_file_detail("101")

    assert result.items[0].file_id == "101"
    assert detail.file_id == "101"
    # 文件夹索引 → 判型 → 文件索引 → 详情。
    assert transport.calls == [
        "fs_files_app",
        "fs_info_app",
        "fs_files_app",
        "fs_info_app",
    ]


@pytest.mark.asyncio
async def test_gateway_falls_back_to_legacy_read_method_only_on_405():
    transport = _AppFirstTransport(
        app_files_error=_MethodNotAllowed("provider response"),
        app_info_error=_MethodNotAllowed("provider response"),
    )
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    result = await gateway.list_directory("7")
    detail = await gateway.get_file_detail("101")

    assert result.items[0].file_id == "101"
    assert detail.file_id == "101"
    # 文件夹索引(405 回退)→ 判型(405 回退)→ 文件索引(405 回退)→ 详情(405 回退)。
    assert transport.calls == [
        "fs_files_app",
        "fs_files",
        "fs_info_app",
        "fs_info",
        "fs_files_app",
        "fs_files",
        "fs_info_app",
        "fs_info",
    ]


@pytest.mark.asyncio
async def test_fs_info_app_empty_list_falls_back_to_legacy_detail():
    """fs_info_app 对文件(fid)请求实测返回空列表,必须回退 legacy 详情。"""
    transport = _AppFirstTransport(app_info_response=[])
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    detail = await gateway.get_file_detail("101")

    assert detail.file_id == "101"
    assert transport.calls == ["fs_info_app", "fs_info"]


@pytest.mark.asyncio
async def test_gateway_fails_closed_when_app_read_method_fails_without_405():
    transport = _AppFirstTransport(
        app_files_error=OSError("network broken"),
        app_info_error=TimeoutError("slow provider"),
    )
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    with pytest.raises(P115ReadOnlyGatewayError, match="fs_files_failed"):
        await gateway.list_directory("7")
    with pytest.raises(P115ReadOnlyGatewayError, match="fs_info_timeout"):
        await gateway.get_file_detail("101")

    assert transport.calls == ["fs_files_app", "fs_info_app"]
