import sys
from types import SimpleNamespace

import pytest

from watch_assistant.services.p115_device_types import P115_DEVICE_CODES
from watch_assistant.services.p115_qrcode import P115QrcodeService

COOKIE = {
    "UID": "uid-one",
    "CID": "cid-one",
    "KID": "kid-one",
    "SEID": "seid-one",
}


@pytest.mark.asyncio
async def test_qrcode_session_keeps_provider_fields_server_side(monkeypatch):
    service = P115QrcodeService()
    calls: list[str] = []
    statuses = iter((0, 2))

    async def fake_provider(action: str, value: object):
        calls.append(action)
        if action == "token":
            return {"state": 1, "data": {"uid": "uid", "time": 1, "sign": "sign", "qrcode": "https://115.com/scan/test"}}
        if action == "status":
            return {"state": 1, "data": {"status": next(statuses)}}
        return {"state": 1, "data": {"cookie": COOKIE}}

    monkeypatch.setattr(service, "_call_provider", fake_provider)
    created = await service.create("web", "测试电脑")
    assert "uid" not in str(created)
    assert "sign" not in str(created)
    assert created["image_data_url"].startswith("data:image/svg+xml;base64,")

    session_id = str(created["session_id"])
    assert (await service.poll(session_id))[0] == "waiting"
    status, cookie = await service.poll(session_id)
    assert status == "ready"
    assert cookie == "UID=uid-one; CID=cid-one; KID=kid-one; SEID=seid-one"
    await service.consume(session_id)
    assert await service.poll(session_id) == ("ready", None)
    assert calls == ["token", "status", "status", "result"]


@pytest.mark.asyncio
async def test_qrcode_supports_all_verified_device_codes(monkeypatch):
    service = P115QrcodeService()
    calls: list[tuple[str, object]] = []

    async def fake_provider(action: str, value: object):
        calls.append((action, value))
        return {"state": 1, "data": {"uid": "uid", "time": 1, "sign": "sign", "qrcode": "https://115.com/scan/test"}}

    monkeypatch.setattr(service, "_call_provider", fake_provider)

    for device_code in P115_DEVICE_CODES:
        await service.create(device_code, f"设备-{device_code}")

    assert calls == [("token", device_code) for device_code in P115_DEVICE_CODES]


@pytest.mark.asyncio
async def test_provider_token_uses_selected_device_code(monkeypatch):
    calls: list[str] = []

    class FakeP115Client:
        @staticmethod
        def login_qrcode_token(*, app: str):
            calls.append(app)
            return {"state": 1, "data": {}}

    monkeypatch.setitem(sys.modules, "p115client", SimpleNamespace(P115Client=FakeP115Client))
    await P115QrcodeService()._call_provider("token", "115android")

    assert calls == ["115android"]
