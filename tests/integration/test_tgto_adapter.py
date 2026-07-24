import json
from pathlib import Path

import httpx
import pytest
import respx

from watch_assistant.adapters.tgto import TgtoDriveClient, TgtoUnsupported
from watch_assistant.schemas import RemoteStatus

BASE_URL = "http://tgto.test"


def _contract():
    return {
        "schema_version": 1,
        "supported": True,
        "login": {
            "method": "POST",
            "path": "/api/login",
            "username_field": "username",
            "password_field": "password",
            "success_field": "success",
        },
        "submit": {
            "method": "POST",
            "path": "/api/tasks",
            "remote_reference_field": "id",
            "url_field": "url",
            "password_field": "password",
        },
        "status": {
            "method": "GET",
            "path_template": "/api/tasks/{remote_reference}",
            "status_field": "status",
        },
        "checks": [{"method": "GET", "path": "/api/tasks/metadata", "accept_status": [200]}],
    }


@respx.mock
async def test_submit_magnet_returns_remote_ref():
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    submit = respx.post(f"{BASE_URL}/api/tasks").mock(
        return_value=httpx.Response(202, json={"id": "remote-123"})
    )
    client = TgtoDriveClient(BASE_URL, "user", "password", _contract())

    result = await client.submit_magnet("magnet:?xt=urn:btih:ABC")

    assert result.accepted is True
    assert result.remote_ref == "remote-123"
    assert submit.calls[0].request.content == b'{"url":"magnet:?xt=urn:btih:ABC"}'
    await client.aclose()


@respx.mock
async def test_status_maps_remote_state():
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.get(f"{BASE_URL}/api/tasks/remote-123").mock(
        return_value=httpx.Response(200, json={"status": "accepted"})
    )
    client = TgtoDriveClient(BASE_URL, "user", "password", _contract())

    result = await client.get_status("remote-123")

    assert result == RemoteStatus.ACCEPTED
    await client.aclose()


@respx.mock
async def test_login_network_failure_is_uncertain_not_needs_auth():
    respx.post(f"{BASE_URL}/api/login").mock(
        side_effect=httpx.ConnectError("password at private URL")
    )
    client = TgtoDriveClient(BASE_URL, "user", "password", _contract())

    result = await client.submit_magnet("magnet:?xt=urn:btih:ABC")

    assert result.status == RemoteStatus.UNCERTAIN
    assert "password" not in (result.error_message or "")
    await client.aclose()


@respx.mock
async def test_confirmed_submit_rejection_is_failed():
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.post(f"{BASE_URL}/api/tasks").mock(
        return_value=httpx.Response(422, json={"message": "invalid"})
    )
    client = TgtoDriveClient(BASE_URL, "user", "password", _contract())

    result = await client.submit_magnet("magnet:?xt=urn:btih:ABC")

    assert result.status == RemoteStatus.FAILED
    await client.aclose()


@pytest.mark.integration
async def test_committed_unsupported_gate_disables_real_push():
    path = Path(__file__).parents[2] / "config" / "tgto-contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    client = TgtoDriveClient("http://tgto.test", "user", "password", contract)

    with pytest.raises(TgtoUnsupported):
        await client.submit_magnet("magnet:?xt=urn:btih:ABC")
    await client.aclose()
