import asyncio

import pytest

from watch_assistant.adapters.p115_library import ScanState, scan_directory
from watch_assistant.adapters.p115_library_gateway import (
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
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls = []

    def fs_files(self, payload):
        self.calls.append(dict(payload))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def __repr__(self) -> str:
        return f"_FsFilesClient(call_count={len(self.calls)})"


def _file(fid="101", parent="7", name="secret-file-name.mkv"):
    return {
        "fc": 1,
        "fid": fid,
        "cid": parent,
        "fn": name,
        "s": "123",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _directory(cid="8", parent="7", name="secret-directory"):
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

        async def fs_files(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return client.fs_files(payload)

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
    assert client.calls == [
        {
            "cid": "7",
            "limit": VERIFIED_PAGE_SIZE,
            "offset": 0,
            "record_open_time": 0,
            "show_dir": 1,
        }
    ]
    assert source.calls == 1
    assert len(factory_calls) == 1
    rendered = repr(gateway) + repr(result) + repr(result.items[0]) + repr(client)
    assert "SYNTHETIC_COOKIE_SECRET" not in rendered
    assert "SYNTHETIC_PICKCODE_SECRET" not in rendered
    assert "secret-file-name.mkv" not in rendered
    assert "101" not in rendered


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
            _page([_directory()], count=1),
            _page([_file("102", parent="8")], count=1),
        )
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    child = await gateway.list_directory("8")

    assert child.items[0].file_id == "102"
    assert [call["cid"] for call in client.calls] == ["7", "8"]


@pytest.mark.asyncio
async def test_mismatched_parent_does_not_expand_observed_directory_scope():
    client = _FsFilesClient((_page([_directory(cid="8", parent="99")]),))
    gateway, source, factory_calls = _gateway(client)

    with pytest.raises(P115ReadOnlyGatewayError, match="entry_scope_unverified"):
        await gateway.list_directory("7")
    with pytest.raises(P115ReadOnlyGatewayError, match="scope_unverified"):
        await gateway.list_directory("8")

    assert client.calls == [
        {
            "cid": "7",
            "limit": VERIFIED_PAGE_SIZE,
            "offset": 0,
            "record_open_time": 0,
            "show_dir": 1,
        }
    ]
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
    def __init__(self, response, pages=()):
        super().__init__(pages)
        self._detail_response = response
        self.detail_calls = []

    def fs_info(self, payload):
        self.detail_calls.append(dict(payload))
        return self._detail_response


@pytest.mark.asyncio
async def test_fs_info_maps_verified_file_detail_without_path_or_repr_leaks():
    client = _DetailClient(
        {
            "state": True,
            "file_category": "1",
            "fid": "101",
            "cid": "7",
            "file_name": "secret-file-name.mkv",
            "size": "123",
            "ptime": "1710000000",
            "pick_code": "SYNTHETIC_PICKCODE_SECRET",
        },
        pages=(_page([_file("101")]),),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")

    result = await gateway.get_file_detail("101")

    assert client.detail_calls == [{"fid": "101"}]
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
            "file_name": "secret-file-name.mkv",
            "size": "123",
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
            "fid": "102",
            "file_name": "secret-file-name.mkv",
            "size": "123",
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
            "cid": "8",
            "pid": "7",
            "file_name": "directory-name",
            "size": "0",
        },
        pages=(_page([_directory("8")]),),
    )
    gateway, _, _ = _gateway(client)

    await gateway.list_directory("7")
    result = await gateway.get_directory_detail("8")

    assert client.detail_calls == [{"cid": "8"}]
    assert result.directory_id == "8"
    assert result.parent_id == "7"


@pytest.mark.asyncio
async def test_gateway_passes_only_the_shared_deadline_remainder_to_transport():
    client = _FsFilesClient((_page([]),))
    source = _CredentialSource()
    clock_values = iter((100.0, 101.0, 102.0, 103.0))

    class Transport:
        def __init__(self):
            self.timeouts = []

        async def fs_files(self, payload, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return client.fs_files(payload)

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
                [_directory(cid="8", parent="0"), _directory(cid="9", parent="0")],
                limit=VIRTUAL_ROOT_PAGE_SIZE - 2,
            ),
        )
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
