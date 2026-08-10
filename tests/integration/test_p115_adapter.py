import asyncio
import os

import pytest

from watch_assistant.adapters.p115 import INFOHASH_REMOTE_REF_PREFIX, P115Adapter
from watch_assistant.adapters.p115_library import LibraryEntry
from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyGatewayError
from watch_assistant.schemas import RemoteObservation, RemoteStatus
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
        delay: float = 0,
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
        self.delay = delay
        self.validation_async_flags = []

    def clouddownload_task_add_url(self, payload, *, async_=False, request=None):
        del async_, request
        self.add_payloads.append(payload)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            import time

            time.sleep(self.delay)
        self.active -= 1
        return self.response

    def share_receive(self, payload, *, async_=False, request=None):
        del async_, request
        self.share_payloads.append(payload)
        if self.receive_error is not None:
            raise self.receive_error
        return self.response

    def share_snap(self, payload, *, async_=False, request=None):
        del async_, request
        if self.share_error is not None:
            raise self.share_error
        self.list_payloads.append({"share_snap": payload})
        index = self.share_snap_calls
        self.share_snap_calls += 1
        if index >= len(self.share_pages):
            return {"state": True, "data": {"list": []}}
        return self.share_pages[index]

    def clouddownload_task_list(self, payload, *, async_=False, request=None):
        del request
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


class FakeReadOnlyGateway:
    def __init__(self, detail=None, *, directory_detail=None, error=None):
        self.detail = detail
        self.directory_detail = directory_detail
        self.error = error
        self.file_ids = []
        self.directory_ids = []

    async def get_file_detail(self, file_id):
        self.file_ids.append(file_id)
        if self.error is not None:
            raise self.error
        return self.detail

    async def get_directory_detail(self, directory_id):
        self.directory_ids.append(directory_id)
        if self.error is not None:
            raise self.error
        return self.directory_detail


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
async def test_submit_magnet_uses_verified_task_target_when_supplied(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client()
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET, target_cid="314159")

    assert result.status == RemoteStatus.ACCEPTED
    assert fake.add_payloads == [{"url": MAGNET, "wp_path_id": "314159"}]
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

    assert status == RemoteStatus.DOWNLOADING
    assert fake.list_payloads == [{"page": 1}]
    assert fake.add_payloads == []
    assert fake.share_payloads == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_available_status_without_file_id_stays_uncertain(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [{"info_hash": infohash, "status": "available"}],
        }
    )
    adapter = P115Adapter(provider, 7, client_factory=lambda _cookie: fake)

    observation = await adapter.get_status("infohash:" + infohash)

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.UNCERTAIN
    assert observation.error_code == "availability_file_id_unavailable"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_available_status_requires_readonly_file_detail_evidence(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [
                {"info_hash": infohash, "status": "available", "fid": "101"}
            ],
        }
    )
    gateway = FakeReadOnlyGateway(
        LibraryEntry(
            directory_id=None,
            file_id="101",
            parent_id="7",
            name="hidden-name.mkv",
            is_directory=False,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
        directory_detail=LibraryEntry(
            directory_id="7",
            file_id=None,
            parent_id=None,
            name="target",
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
    )
    adapter = P115Adapter(
        provider,
        7,
        client_factory=lambda _cookie: fake,
        readonly_gateway=gateway,
    )

    observation = await adapter.get_status("infohash:" + infohash)

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.AVAILABLE
    assert observation.file_id == "101"
    assert observation.parent_id == "7"
    assert observation.is_directory is False
    assert observation.availability_verified is True
    assert gateway.file_ids == ["101"]
    assert gateway.directory_ids == ["7"]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_available_status_rejects_wrong_parent_from_readonly_detail(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [
                {"info_hash": infohash, "status": "available", "fid": "101"}
            ],
        }
    )
    gateway = FakeReadOnlyGateway(
        LibraryEntry(
            directory_id=None,
            file_id="101",
            parent_id="8",
            name="hidden-name.mkv",
            is_directory=False,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
        directory_detail=LibraryEntry(
            directory_id="7",
            file_id=None,
            parent_id=None,
            name="target",
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
    )
    adapter = P115Adapter(
        provider,
        7,
        client_factory=lambda _cookie: fake,
        readonly_gateway=gateway,
    )

    observation = await adapter.get_status("infohash:" + infohash)

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.UNCERTAIN
    assert observation.error_code == "availability_parent_mismatch"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_available_status_requires_readonly_parent_directory_evidence(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [
                {"info_hash": infohash, "status": "available", "fid": "101"}
            ],
        }
    )
    gateway = FakeReadOnlyGateway(
        LibraryEntry(
            directory_id=None,
            file_id="101",
            parent_id="7",
            name="hidden-name.mkv",
            is_directory=False,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
        directory_detail=LibraryEntry(
            directory_id="8",
            file_id=None,
            parent_id=None,
            name="wrong-target",
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
    )
    adapter = P115Adapter(
        provider,
        7,
        client_factory=lambda _cookie: fake,
        readonly_gateway=gateway,
    )

    observation = await adapter.get_status("infohash:" + infohash)

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.UNCERTAIN
    assert observation.error_code == "availability_parent_mismatch"
    assert gateway.file_ids == ["101"]
    assert gateway.directory_ids == ["7"]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_available_status_timeout_stays_uncertain_and_redacted(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [
                {"info_hash": infohash, "status": "available", "fid": "101"}
            ],
        }
    )
    gateway = FakeReadOnlyGateway(
        error=P115ReadOnlyGatewayError("request_timeout")
    )
    adapter = P115Adapter(
        provider,
        7,
        client_factory=lambda _cookie: fake,
        readonly_gateway=gateway,
    )

    observation = await adapter.get_status_for_task(
        "infohash:" + infohash, target_directory_id="7"
    )

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.UNCERTAIN
    assert observation.error_code == "availability_observer_timeout"
    assert observation.file_id is None
    assert observation.parent_id is None
    assert observation.is_directory is None
    assert gateway.file_ids == ["101"]
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
async def test_business_auth_words_do_not_trigger_credentials_reauth(tmp_path):
    # 回归(L7):业务错误里带"授权"等字样(如文件/分享权限提示)不得被
    # 子串匹配误判为登录失效;否则用户会被反复要求重新登录而问题依旧。
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(
        response={"state": False, "error": "该文件未获得授权下载", "errno": 0}
    )
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "submit_rejected"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_explicit_login_expiry_message_still_maps_to_needs_auth(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(response={"state": False, "error": "请重新登录"})
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.NEEDS_AUTH
    await adapter.aclose()


@pytest.mark.asyncio
async def test_authentication_exception_class_still_maps_to_needs_auth(tmp_path):
    provider, _path = _provider(tmp_path)
    # 与 p115client 的真实异常类名一致:P115AuthenticationError。
    auth_error = type("P115AuthenticationError", (Exception,), {})
    fake = FakeP115Client(share_error=auth_error())
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.NEEDS_AUTH
    await adapter.aclose()


@pytest.mark.asyncio
async def test_app_auth_limit_exception_is_not_credentials_reauth(tmp_path):
    # 回归(L7):类名里嵌入 "Auth" 的授权限制异常(P115OpenAppAuthLimitExceeded,
    # 授权应用数达上限)不是登录失效,不应触发 NEEDS_AUTH。
    provider, _path = _provider(tmp_path)
    limit_error = type("P115OpenAppAuthLimitExceeded", (Exception,), {})
    fake = FakeP115Client(share_error=limit_error())
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.save_share("https://115.com/s/code", None)

    assert result.status == RemoteStatus.FAILED
    assert result.error_code == "share_listing_failed"
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

    assert await adapter.get_status("infohash:" + infohash) == RemoteStatus.UNCERTAIN
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
        (0, 0, RemoteStatus.UNCERTAIN),
        (2, 1, RemoteStatus.UNCERTAIN),
        ("queued", 0, RemoteStatus.UNCERTAIN),
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


@pytest.mark.asyncio
async def test_available_status_allows_root_target_cid_zero(tmp_path):
    """cid=0 (115 root) must verify as AVAILABLE, not parent mismatch."""
    provider, _path = _provider(tmp_path)
    infohash = "d" * 40
    fake = FakeP115Client(
        task_response={
            "state": True,
            "data": [{"info_hash": infohash, "status": "available", "fid": "101"}]
        }
    )
    gateway = FakeReadOnlyGateway(
        LibraryEntry(
            directory_id=None,
            file_id="101",
            parent_id="0",
            name="root.mkv",
            is_directory=False,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
        directory_detail=LibraryEntry(
            directory_id="0",
            file_id=None,
            parent_id=None,
            name="root",
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
        ),
    )
    adapter = P115Adapter(
        provider,
        0,
        client_factory=lambda _cookie: fake,
        readonly_gateway=gateway,
    )

    observation = await adapter.get_status("infohash:" + infohash)

    assert isinstance(observation, RemoteObservation)
    assert observation.status is RemoteStatus.AVAILABLE
    assert observation.availability_verified is True
    assert observation.parent_id == "0"
    assert gateway.directory_ids == ["0"]


class _BusyOnceClient(FakeP115Client):
    """第一次写调用抛 errno=990009(服务端仍在处理上次提交),第二次成功。"""

    def __init__(self):
        super().__init__()
        self.busy_calls = 0

    def clouddownload_task_add_url(self, payload, *, async_=False, request=None):
        del async_, request
        self.add_payloads.append(payload)
        self.busy_calls += 1
        if self.busy_calls == 1:
            error = RuntimeError("busy: previous request still processing")
            error.errno = 990009
            raise error
        return self.response


class _AlwaysBusyClient(FakeP115Client):
    def __init__(self):
        super().__init__()
        self.busy_calls = 0

    def clouddownload_task_add_url(self, payload, *, async_=False, request=None):
        del async_, request
        self.add_payloads.append(payload)
        self.busy_calls += 1
        error = RuntimeError("busy")
        error.errno = 990009
        raise error


@pytest.mark.asyncio
async def test_submit_magnet_hangs_are_bounded_by_timeout_and_marked_uncertain(tmp_path):
    """修复前:写调用裸 to_thread 无超时,挂起会拖死 worker 且任务永久卡 SUBMITTING。"""
    import time as time_module

    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(delay=3)
    adapter = P115Adapter(
        provider, 42,
        client_factory=lambda _cookie: fake,
        request_timeout_seconds=0.2,
    )

    started = time_module.monotonic()
    result = await adapter.submit_magnet(MAGNET)
    elapsed = time_module.monotonic() - started

    assert result.status == RemoteStatus.UNCERTAIN
    assert result.error_code == "timeout"  # 与 submit_ambiguous 区分,优先走只读核对
    assert elapsed < 2.0  # 未等待挂起线程返回
    await adapter.aclose()


@pytest.mark.asyncio
async def test_submit_magnet_retries_busy_990009_once_and_returns_deterministic(tmp_path):
    """990009 = 服务端仍在处理上次提交;幂等重试一次拿到确定性结果。"""
    provider, _path = _provider(tmp_path)
    fake = _BusyOnceClient()
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert fake.busy_calls == 2
    assert result.status == RemoteStatus.ACCEPTED
    assert result.remote_ref == "task-1"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_submit_magnet_busy_after_retry_stays_ambiguous(tmp_path):
    provider, _path = _provider(tmp_path)
    fake = _AlwaysBusyClient()
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert fake.busy_calls == 2  # 只重试一次,不无限重试
    assert result.status == RemoteStatus.UNCERTAIN
    assert result.error_code == "submit_ambiguous"
    await adapter.aclose()
