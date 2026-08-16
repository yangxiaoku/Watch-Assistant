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
    """单索引 mock:show_dir=1 列表 + fs_info 判型。

    真实 115(2026-08-14 实测):目录列表记录对文件与目录完全同形(fc 恒 0 或
    "0"、无 fid/is_dir),只能以 fs_info 判型:count>0 或 folder_count>0 即目录
    (纯文件目录的 folder_count=0,count 是最稳定的目录信号)。
    """

    def __init__(self, responses, *, directories=()) -> None:
        self._responses = list(responses)
        self._directories = set(directories)
        self.calls = []
        self.detail_calls = []

    def fs_files(self, payload):
        self.calls.append(dict(payload))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def fs_info(self, payload):
        self.detail_calls.append(dict(payload))
        value = str(payload.get("cid") or payload.get("fid") or "")
        is_directory = value in self._directories
        return {
            "state": True,
            "count": "2" if is_directory else 0,
            "folder_count": 1 if is_directory else 0,
            "play_long": 0,
            "size": "0B",
            "file_name": "classified-entry",
            "paths": [],
        }

    def __repr__(self) -> str:
        return f"_FsFilesClient(call_count={len(self.calls)})"


def _file(fid="101", parent="7", name="secret-file-name.mkv"):
    """app 目录列表形态:文件与目录同形(fid+fc=\"0\"),需 fs_info 判型。"""
    return {
        "fc": "0",
        "fid": fid,
        "pid": parent,
        "fn": name,
        "s": "123",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _folder_entry(cid="8", parent="7", name="secret-directory"):
    """legacy 目录列表形态:文件与目录同形(fc=0、无 fid),需 fs_info 判型。"""
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
    client = _FsFilesClient((_page([_file()]),))
    gateway, source, factory_calls = _gateway(client)

    result = await gateway.list_directory("7")

    assert result.scan_complete is True
    assert result.terminal is True
    assert result.has_more is False
    assert result.items[0].pickcode == "SYNTHETIC_PICKCODE_SECRET"
    assert result.items[0].path is None
    assert client.calls == [_folder_payload("7")]
    assert client.detail_calls == [{"fid": "101"}]
    assert source.calls == 1
    assert len(factory_calls) == 1
    rendered = repr(gateway) + repr(result) + repr(result.items[0]) + repr(client)
    assert "SYNTHETIC_COOKIE_SECRET" not in rendered
    assert "SYNTHETIC_PICKCODE_SECRET" not in rendered
    assert "secret-file-name.mkv" not in rendered
    assert "101" not in rendered


@pytest.mark.asyncio
async def test_listed_file_and_directory_are_classified_via_fs_info_count():
    client = _FsFilesClient(
        (
            _page(
                [
                    _file(fid="101", name="pushed.mkv"),
                    _folder_entry(cid="8", name="subdir"),
                ],
                count=2,
                limit=VERIFIED_BATCH_PAGE_SIZE,
            ),
        ),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory("7", page_size=VERIFIED_BATCH_PAGE_SIZE)

    assert client.calls == [_folder_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE)]
    assert client.detail_calls == [{"fid": "101"}, {"cid": "8"}]
    assert result.items[0].is_directory is False
    assert result.items[0].file_id == "101"
    assert result.items[0].parent_id == "7"
    assert result.items[1].is_directory is True
    assert result.items[1].directory_id == "8"
    assert result.items[1].parent_id == "7"
    assert result.terminal is True
    assert result.total == 2


@pytest.mark.asyncio
async def test_file_style_record_classifies_by_fid_not_parent_cid():
    """file-style 记录带 fid 且 cid=直接父级 id;判型必须用 fid,否则文件会被
    误判为目录(父目录 count>0),树扫描产生目录环。"""
    client = _FsFilesClient(
        (
            _page(
                [
                    {
                        "fc": 1,
                        "fid": "101",
                        "cid": "8",
                        "n": "episode.mkv",
                        "s": "1",
                        "play_long": 0,
                    }
                ],
                count=1,
            ),
        ),
    )
    gateway, _, _ = _gateway(client, authorized_directory_ids=("8",))

    result = await gateway.list_directory("8")

    assert client.detail_calls == [{"fid": "101"}]
    assert result.items[0].is_directory is False
    assert result.items[0].file_id == "101"
    assert result.items[0].parent_id == "8"


@pytest.mark.asyncio
async def test_batch_page_size_reads_full_page_in_one_call():
    records = [_file(fid=str(101 + i), name=f"file-{i}.mkv") for i in range(3)]
    client = _FsFilesClient(
        (_page(records, offset=0, count=3, limit=VERIFIED_BATCH_PAGE_SIZE),)
    )
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory("7", page_size=VERIFIED_BATCH_PAGE_SIZE)

    assert client.calls == [_folder_payload("7", limit=VERIFIED_BATCH_PAGE_SIZE)]
    assert len(result.items) == 3
    assert result.terminal is True
    assert result.has_more is False
    assert result.next_page is None


@pytest.mark.asyncio
async def test_batch_page_size_paginates_with_batch_offsets():
    client = _FsFilesClient(
        (
            _page([_file(fid="201")], offset=0, count=51, limit=VERIFIED_BATCH_PAGE_SIZE),
            _page([_file(fid="202")], offset=50, count=51, limit=VERIFIED_BATCH_PAGE_SIZE),
        )
    )
    gateway, _, _ = _gateway(client)

    page1 = await gateway.list_directory("7", page=1, page_size=VERIFIED_BATCH_PAGE_SIZE)
    page2 = await gateway.list_directory("7", page=2, page_size=VERIFIED_BATCH_PAGE_SIZE)

    assert page1.has_more is True
    assert page1.next_page == 2
    assert page2.terminal is True
    assert page2.has_more is False
    assert [call["offset"] for call in client.calls] == [0, 50]


@pytest.mark.asyncio
async def test_verified_app_listing_compact_pc_maps_to_pickcode():
    record = _file()
    record.pop("pick_code")
    record["pc"] = "SYNTHETIC_PICKCODE_SECRET"
    client = _FsFilesClient((_page([record]),))
    gateway, _, _ = _gateway(client)

    result = await gateway.list_directory("7")

    assert result.items[0].pickcode == "SYNTHETIC_PICKCODE_SECRET"
    assert "SYNTHETIC_PICKCODE_SECRET" not in repr(result.items[0])


@pytest.mark.asyncio
async def test_consecutive_offset_count_pages_complete_only_at_verified_terminal_page():
    client = _FsFilesClient(
        (
            _page([_file("101", name="one.mkv")], offset=0, count=2),
            _page([_file("102", name="two.mkv")], offset=1, count=2),
        )
    )
    gateway, _, _ = _gateway(client)

    scan = await scan_directory(gateway, "7", page_size=VERIFIED_PAGE_SIZE)

    assert scan.state is ScanState.COMPLETE
    assert scan.scan_complete is True
    assert scan.pages_read == 2
    assert [call["offset"] for call in client.calls] == [0, 1]


@pytest.mark.asyncio
async def test_observed_child_directory_can_be_scanned_without_expanding_scope():
    client = _FsFilesClient(
        (
            _page([_folder_entry()], count=1),
            _page([_file("102", parent="8")], count=1),
        ),
        directories=("8",),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    child = await gateway.list_directory("8")

    assert child.items[0].file_id == "102"
    assert [call["cid"] for call in client.calls] == ["7", "8"]
    assert client.detail_calls == [{"cid": "8"}, {"fid": "102"}]


@pytest.mark.asyncio
async def test_mismatched_parent_does_not_expand_observed_directory_scope():
    client = _FsFilesClient(
        (_page([_folder_entry(cid="8", parent="99")]),), directories=("8",)
    )
    gateway, source, factory_calls = _gateway(client)

    with pytest.raises(P115ReadOnlyGatewayError, match="entry_scope_unverified"):
        await gateway.list_directory("7")
    with pytest.raises(P115ReadOnlyGatewayError, match="scope_unverified"):
        await gateway.list_directory("8")

    assert client.calls == [_folder_payload("7")]
    assert source.calls == 1
    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_restored_child_allowlist_is_usable_by_a_new_gateway_instance():
    client = _FsFilesClient((_page([_file("102", parent="8")]),))
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
            "count": 1 if value in self._directories else 0,
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
            "count": "3",
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
            "count": "3",
            "folder_count": 3,
            "cid": "8",
            "pid": "7",
            "file_name": "secret-directory",
            "size": "0",
        },
        pages=(_page([_folder_entry("8")]),),
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
async def test_directory_detail_anchors_identity_via_paths_parent():
    """磁力推送创建的是同名目录:目录详情与文件详情一样以 paths 父链锚定身份,
    并把父目录登记为已观察目录。"""
    client = _DetailClient(
        details={
            "101": {
                "state": True,
                "file_name": "pushed-torrent-dir",
                "count": "3",
                "folder_count": 0,
                "play_long": 10731,
                "size": "112GB",
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
        authorized_directory_ids=("7", "101"),
        authorized_file_ids=("101",),
    )

    detail = await gateway.get_directory_detail("101")
    parent = await gateway.get_directory_detail("7")

    assert detail.directory_id == "101"
    assert detail.parent_id == "7"
    assert detail.is_directory is True
    assert parent.directory_id == "7"
    assert parent.is_directory is True
    assert client.detail_calls == [{"cid": "101"}, {"cid": "7"}]


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
    client = _FsFilesClient((_page([]),))
    source = _CredentialSource()
    clock_values = iter((100.0, 101.0, 102.0, 103.0))

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

    assert transport.timeouts == [2.0]


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
    gateway, _, _ = _gateway(_FsFilesClient((response,)))

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
        self._detail = {
            "state": True,
            "file_category": "1",
            "folder_count": 0,
            "fid": "101",
            "cid": "7",
            "file_name": "secret-file-name.mkv",
        }

    async def fs_files_app(self, payload, *, timeout_seconds):
        self.calls.append("fs_files_app")
        if self._app_files_error is not None:
            raise self._app_files_error
        return self._files_page

    async def fs_files(self, payload, *, timeout_seconds):
        self.calls.append("fs_files")
        return self._files_page

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
    # 列表 → 判型 → 详情。
    assert transport.calls == ["fs_files_app", "fs_info_app", "fs_info_app"]


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
    # 列表(405 回退)→ 判型(405 回退)→ 详情(405 回退)。
    assert transport.calls == [
        "fs_files_app",
        "fs_files",
        "fs_info_app",
        "fs_info",
        "fs_info_app",
        "fs_info",
    ]


class _Flaky429Transport:
    """前 N 次调用抛 HTTPError 429(风控/限流),之后正常。"""

    def __init__(self, page, *, failures=1):
        self._page = page
        self._failures = failures
        self.calls = []

    async def fs_files_app(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        self.calls.append("fs_files_app")
        if self._failures > 0:
            self._failures -= 1
            from urllib.error import HTTPError

            raise HTTPError(
                "https://proapi.115.com/android/ufile/files",
                429,
                "Too Many Requests",
                None,
                None,
            )
        return self._page

    async def fs_files(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        self.calls.append("fs_files")
        return self._page

    async def fs_info_app(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        self.calls.append("fs_info_app")
        return {
            "state": True,
            "count": 0,
            "folder_count": 0,
            "play_long": 0,
            "size": "0B",
            "file_name": "classified",
            "paths": [],
        }

    async def fs_info(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        self.calls.append("fs_info")
        return {
            "state": True,
            "count": 0,
            "folder_count": 0,
            "play_long": 0,
            "size": "0B",
            "file_name": "classified",
            "paths": [],
        }


@pytest.mark.asyncio
async def test_gateway_retries_transient_429_without_changing_result():
    """429(风控/限流)退避重试;恢复后正常返回,不因瞬时限流失败。"""
    transport = _Flaky429Transport(_page([_file()]), failures=1)
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
    )

    result = await gateway.list_directory("7")

    assert result.items[0].file_id == "101"
    # 列表(429 重试一次) + 判型。
    assert transport.calls == ["fs_files_app", "fs_files_app", "fs_info_app"]


@pytest.mark.asyncio
async def test_gateway_gives_up_after_retry_budget_on_persistent_429():
    """持续 429 超过重试预算后失败关闭,不无限重试。"""
    transport = _Flaky429Transport(_page([_file()]), failures=10)
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
    )

    with pytest.raises(P115ReadOnlyGatewayError, match="fs_files_failed"):
        await gateway.list_directory("7")

    assert len(transport.calls) == 3


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
    """非 405 且非超时的 app 端点异常仍失败关闭(不静默回退旧接口)。"""
    transport = _AppFirstTransport(app_files_error=OSError("network broken"))
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    with pytest.raises(P115ReadOnlyGatewayError, match="fs_files_failed"):
        await gateway.list_directory("7")

    assert transport.calls == ["fs_files_app"]


@pytest.mark.asyncio
async def test_gateway_falls_back_to_legacy_on_app_timeout():
    """proapi「黑洞」(连接建立但无 HTTP 响应)表现为超时:回退 legacy 完成读取。

    2026-08-16 实测批量调用后 proapi 进入黑洞期,等满超时会整页失败;
    超时回退 webapi 是风控窗口内不拖垮扫描的关键路径。
    """
    transport = _AppFirstTransport(app_info_error=TimeoutError("slow provider"))
    gateway = P115ReadOnlyDirectoryGateway(
        _CredentialSource(),
        lambda _credential: transport,
        authorized_directory_ids=("7",),
        authorized_file_ids=("101",),
    )

    detail = await gateway.get_file_detail("101")

    assert detail.file_id == "101"
    assert transport.calls == ["fs_info_app", "fs_info"]
