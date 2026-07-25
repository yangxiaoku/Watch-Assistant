import asyncio
import os

import pytest

from watch_assistant.adapters.p115 import INFOHASH_REMOTE_REF_PREFIX, P115Adapter
from watch_assistant.schemas import RemoteStatus
from watch_assistant.services.p115_credentials import CookieProvider

COOKIE = "UID=123_A1_456; CID=cid; KID=kid; SEID=seid"
MAGNET = "magnet:?xt=urn:btih:" + ("a" * 40)


class FakeP115Client:
    def __init__(self, response=None, *, task_response=None):
        self.response = response or {"state": True, "data": {"task_id": "task-1"}}
        self.task_response = task_response or {"state": True, "data": []}
        self.add_payloads = []
        self.share_payloads = []
        self.list_payloads = []
        self.active = 0
        self.max_active = 0
        self.delay = 0

    def clouddownload_task_add_urls(self, payload):
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
        return self.response

    def clouddownload_task_list(self, payload):
        self.list_payloads.append(payload)
        return self.task_response


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
    assert fake.add_payloads == [{"urls": MAGNET, "wp_path_id": 42}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_submit_magnet_freezes_infohash_remote_ref_fallback(tmp_path):
    provider, _path = _provider(tmp_path)
    infohash = "c" * 40
    fake = FakeP115Client(response={"state": True, "data": {"info_hash": infohash}})
    adapter = P115Adapter(provider, 42, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == RemoteStatus.ACCEPTED
    assert result.remote_ref == INFOHASH_REMOTE_REF_PREFIX + infohash
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
    assert fake.share_payloads == [
        {
            "share_code": "share-code",
            "receive_code": "abcd",
            "file_id": "0",
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
    ("response", "expected"),
    [
        ({"state": False, "errno": 99}, RemoteStatus.NEEDS_AUTH),
        ({"state": False, "error": "无需重复接收"}, RemoteStatus.ACCEPTED),
    ],
)
async def test_auth_and_idempotent_responses_are_mapped(tmp_path, response, expected):
    provider, _path = _provider(tmp_path)
    fake = FakeP115Client(response=response)
    adapter = P115Adapter(provider, 1, client_factory=lambda _cookie: fake)

    result = await adapter.submit_magnet(MAGNET)

    assert result.status == expected
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
