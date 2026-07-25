import asyncio
import os

import pytest

from watch_assistant.adapters.p115 import INFOHASH_REMOTE_REF_PREFIX, P115Adapter
from watch_assistant.schemas import RemoteStatus
from watch_assistant.services.p115_credentials import CookieProvider

COOKIE = "UID=123_A1_456; CID=cid; KID=kid; SEID=seid"
MAGNET = "magnet:?xt=urn:btih:" + ("a" * 40)


class FakeP115Client:
    def __init__(
        self,
        response=None,
        *,
        task_response=None,
        task_pages=None,
        share_pages=None,
        share_error=None,
        receive_error=None,
    ):
        self.response = response or {"state": True, "data": {"task_id": "task-1"}}
        self.task_response = task_response or {"state": True, "data": []}
        self.task_pages = task_pages
        self.task_page_calls = 0
        self.share_pages = share_pages or [
            {
                "state": True,
                "data": {
                    "list": [{"fid": "101"}, {"cid": "202"}],
                    "total": 2,
                },
            }
        ]
        self.share_error = share_error
        self.receive_error = receive_error
        self.add_payloads = []
        self.share_payloads = []
        self.list_payloads = []
        self.share_snap_calls = 0
        self.active = 0
        self.max_active = 0
        self.delay = 0
        self.validation_async_flags = []

    def clouddownload_task_add_url(self, payload):
        self.add_payloads.append(payload)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            import time

            time.sleep(self.delay)
        self.active -= 1
        return self.response

    def share_receive(self, payload):
        self.share_payloads.append(payload)
        if self.receive_error is not None:
            raise self.receive_error
        return self.response

    def share_snap(self, payload):
        if self.share_error is not None:
            raise self.share_error
        self.list_payloads.append({"share_snap": payload})
        index = self.share_snap_calls
        self.share_snap_calls += 1
        if index >= len(self.share_pages):
            return {"state": True, "data": {"list": []}}
        return self.share_pages[index]

    def clouddownload_task_list(self, payload, *, async_=False):
        self.validation_async_flags.append(async_)
        self.list_payloads.append(payload)
        if self.task_pages is not None:
            response = self.task_pages[self.task_page_calls]
            self.task_page_calls += 1
        else:
            response = self.task_response
        if not async_:
            return response

        async def deliver() -> dict:
            return response

        return deliver()


def _provider(tmp_path, value=COOKIE):
    path = tmp_path / "cookie"
    path.write_text(value, encoding="ascii")
    if os.name != "nt":
        path.chmod(0o600)
    return CookieProvider(path), path


@pytest.mark.asyncio
async def test_submit_magnet_uses_fixed_target_and_remote_task_id(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client()
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.ACCEPTED
    assert result.remote_ref == "task-1"
    assert fake.add_payloads == [{"url": MAGNET, "wp_path_id": 42}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_submit_magnet_freezes_infohash_remote_ref_fallback(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(response={"state": True, "data": {}})
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.ACCEPTED
    assert result.remote_ref == INFOHASH_REMOTE_REF_PREFIX + "a" * 40
    await adapter.aclose()


@pytest.mark.asyncio
async def test_save_share_accepts_allowed_hosts_and_cannot_override_cid(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client()
    adapter = P115Adapter(provider, "99", client_factory=lambda _cookie: fake)

    result = await adapter.save_share(
        "https://anxia.com/s/share-code?cid=other", "abcd"
    )

    assert result.status == RemoteStatus.ACCEPTED
    assert fake.list_payloads == [
        {
            "share_snap": {
                "share_code": "share-code",
                "receive_code": "abcd",
                "cid": 0,
                "limit": 100,
                "offset": 0,
            }
        }
    ]
    assert fake.share_payloads == [
        {
            "share_code": "share-code",
            "receive_code": "abcd",
            "file_id": "101,202",
            "cid": "99",
        }
    ]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_invalid_inputs_and_missing_cookie_never_call_client(tmp_path):
    provider, _path = _provider(tmp_path, "invalid")
    fake = FakeP115Client()
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    invalid_magnet = await adapter.submit_magnet("magnet:?xt=urn:btih:not-a-hash")
    missing_cookie = await adapter.save_share("https://115.com/s/code", None)

    assert invalid_magnet.status == RemoteStatus.FAILED
    assert invalid_magnet.error_code == "invalid_magnet"
    assert missing_cookie.status == RemoteStatus.NEEDS_AUTH
    assert fake.add_payloads == []
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_client_construction_failure_is_failed_and_does_not_submit(tmp_path):
    provider, _path = _provider(tmp_path)
    fake_calls = []

    def factory(_cookie):
        fake_calls.append(True)
        raise PermissionError("cache directory is not writable")

    adapter = P115Adapter(provider, 1, client_factory=factory)

    assert await adapter.ensure_available() is False
    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "adapter_unavailable"
    assert result.remote_ref is None
    assert fake_calls == [True, True]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_missing_cookie_readiness_stays_needs_auth(tmp_path):
    provider = CookieProvider(tmp_path / "missing-cookie")
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: FakeP115Client())

    assert await adapter.ensure_available() is False
    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.NEEDS_AUTH
    assert result.remote_ref is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_validation_uses_one_native_async_task_list_probe(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(task_response={"state": True, "data": []})
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    await adapter.validate_read_only()

    assert fake.validation_async_flags == [True]
    assert fake.list_payloads == [{"page": 1}]
    assert fake.add_payloads == []
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_share_client_construction_failure_is_failed_without_receive(tmp_path):
    provider, _path = _provider(tmp_path)

    def factory(_cookie):
        raise PermissionError("cache directory is not writable")

    adapter = P115Adapter(provider, 1, client_factory=factory)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "adapter_unavailable"
    assert result.remote_ref is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cookie_change_rebuilds_client_without_exposing_cookie(tmp_path):
    provider, path = _provider(tmp_path)
    clients = []

    def factory(_cookie):
        client = FakeP115Client()
        clients.append(client)
        return client

    adapter = P115Adapter(provider, 1, client_factory=factory)
    assert (await adapter.submit_magnet(MAGNET)).status == RemoteStatus.ACCEPTED
    path.write_text("UID=999_A1_456; CID=new; KID=new; SEID=new", encoding="ascii")
    assert (await adapter.submit_magnet(MAGNET)).status == RemoteStatus.ACCEPTED

    assert len(clients) == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_status_queries_task_list_and_supports_infohash_fallback(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "b" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [{"info_hash": infohash, "status": "downloading"}],
        }
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    status = await adapter.get_status("infohash:" + infohash)

    assert status == RemoteStatus.ACCEPTED
    assert fake.list_payloads == [{"page": 1}]
    assert fake.add_payloads == []
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected", "remote_ref"),
    [
        ({"state": False, "errno": 99}, RemoteStatus.NEEDS_AUTH, None),
        (
            {"state": False, "error": "无需重复接收"},
            RemoteStatus.ACCEPTED,
            INFOHASH_REMOTE_REF_PREFIX + "a" * 40,
        ),
        (
            {
                "state": False,
                "error": "already exist",
                "data": {"task_id": "existing-task"},
            },
            RemoteStatus.ACCEPTED,
            "existing-task",
        ),
    ],
)
async def test_auth_and_idempotent_responses_are_mapped(
    tmp_path, response, expected, remote_ref
):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(response=response)
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == expected
    assert result.remote_ref == remote_ref
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_serializes_calls_by_default(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client()
    fake.delay = 0.02
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    await asyncio.gather(
        adapter.submit_magnet(MAGNET),
        adapter.submit_magnet(MAGNET),
        adapter.submit_magnet(MAGNET),
    )

    assert fake.max_active == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_status_searches_later_pages_without_scanning_names(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "d" * 40
    fake = FakeP115Client(
        task_pages=[
            {
                "state": True,
                "data": [{"name": infohash, "status": 9}],
            },
            {
                "state": True,
                "data": [{"info_hash": infohash, "status": 2}],
            },
        ]
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    assert await adapter.get_status("infohash:" + infohash) == RemoteStatus.ACCEPTED
    assert fake.list_payloads == [{"page": 1}, {"page": 2}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_status_move_minus_one_is_failed_but_status_two_is_not(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "e" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [{"info_hash": infohash, "status": 2, "move": -1}],
        }
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)
    assert await adapter.get_status("infohash:" + infohash) == RemoteStatus.FAILED
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_value", "move_value", "expected"),
    [
        (-1, 0, RemoteStatus.FAILED),
        ("-1", "0", RemoteStatus.FAILED),
        (0, 0, RemoteStatus.ACCEPTED),
        (2, 1, RemoteStatus.ACCEPTED),
    ],
)
async def test_status_mapping_uses_observed_numeric_values(
    tmp_path, status_value, move_value, expected
):
    provider, _path = _provider(tmp_path)
    infohash = "f" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [
                {"info_hash": infohash, "status": status_value, "move": move_value}
            ],
        }
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    assert await adapter.get_status("infohash:" + infohash) == expected

    await adapter.aclose()


@pytest.mark.asyncio
async def test_share_listing_is_paged_and_receives_real_top_level_ids(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(
        response={"state": True, "data": {}},
        share_pages=[
            {
                "state": True,
                "data": {"list": [{"fid": "10"}], "has_more": True},
            },
            {
                "state": True,
                "data": {"list": [{"cid": "20"}], "has_more": False},
            },
        ],
    )
    adapter = P115Adapter(provider, 99, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", "pass")

    assert result.status == RemoteStatus.ACCEPTED
    assert result.remote_ref is None
    assert fake.share_payloads == [
        {"share_code": "code", "receive_code": "pass", "file_id": "10,20", "cid": 99}
    ]
    assert [call["share_snap"]["offset"] for call in fake.list_payloads] == [0, 1]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_empty_or_unauthorized_share_fails_without_receive(tmp_path):
    provider, _path = _provider(tmp_path)
    empty = FakeP115Client(
        response={"state": True, "data": {}},
        share_pages=[{"state": True, "data": {"list": []}}],
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: empty)
    result = await adapter.save_share("https://115.com/s/code", None)
    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "empty_share"
    assert empty.share_payloads == []
    await adapter.aclose()

    unauthorized = FakeP115Client(share_pages=[{"state": False, "errno": 99}])
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: unauthorized)
    result = await adapter.save_share("https://115.com/s/code", None)
    assert result.status == RemoteStatus.NEEDS_AUTH
    assert unauthorized.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_exactly_max_share_items_are_received(tmp_path):
    provider, _path = _provider(tmp_path)
    records = [{"fid": str(index)} for index in range(1000)]
    fake = FakeP115Client(
        response={"state": True, "data": {}},
        share_pages=[{"state": True, "data": {"list": records, "total": 1000}}],
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.ACCEPTED
    assert len(fake.share_payloads) == 1
    assert len(fake.share_payloads[0]["file_id"].split(",")) == 1000
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        {"state": True, "data": {"list": [{"fid": "1"}], "count": 1001}},
        {
            "state": True,
            "data": {
                "list": [{"fid": str(index)} for index in range(1000)],
                "has_more": True,
            },
        },
    ],
)
async def test_share_limit_failure_never_receives(tmp_path, page):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(response={"state": True, "data": {}}, share_pages=[page])
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "share_too_large"
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_invalid_later_share_item_never_receives(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(
        share_pages=[
            {"state": True, "data": {"list": [{"fid": "1"}], "has_more": True}},
            {"state": True, "data": {"list": [{"name": "missing-id"}]}},
        ]
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "malformed_share_listing"
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_nonadvancing_share_cursor_fails_without_looping(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(
        share_pages=[
            {
                "state": True,
                "data": {
                    "list": [{"fid": "1"}],
                    "has_more": True,
                    "next_offset": 0,
                },
            }
        ]
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "malformed_share_listing"
    assert fake.share_snap_calls == 1
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_share_listing_exception_is_failed_but_receive_exception_is_uncertain(
    tmp_path,
):
    provider, _path = _provider(tmp_path)
    listing = FakeP115Client(share_error=OSError("listing unavailable"))
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: listing)
    result = await adapter.save_share("https://115.com/s/code", None)
    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "share_listing_failed"
    assert listing.share_payloads == []
    await adapter.aclose()

    receiving = FakeP115Client(receive_error=OSError("receive unavailable"))
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: receiving)
    result = await adapter.save_share("https://115.com/s/code", None)
    assert result.status == RemoteStatus.UNCERTAIN
    assert len(receiving.share_payloads) == 1
    await adapter.aclose()
