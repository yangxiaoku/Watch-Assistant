import pytest

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
