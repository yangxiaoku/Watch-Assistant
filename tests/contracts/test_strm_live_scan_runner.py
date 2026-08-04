import pytest

from scripts.p115_strm_application_live_runner import _scan as application_scan
from scripts.p115_strm_playback_live_runner import _scan as playback_scan


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class _Client:
    def __init__(self, status_body):
        self.status_body = status_body
        self.get_calls = []

    async def post(self, path, *, headers, json):
        del path, headers, json
        return _Response({"run_id": "scan-run", "state": "queued", "complete": False})

    async def get(self, path, *, headers):
        del headers
        self.get_calls.append(path)
        return _Response(self.status_body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scan", "path"),
    (
        (application_scan, "/api/v1/libraries/strm-live/scans/scan-run"),
        (playback_scan, "/api/v1/libraries/strm-playback-live/scans/scan-run"),
    ),
)
async def test_strm_live_scan_waits_for_queued_operation(scan, path, monkeypatch):
    client = _Client(
        {"run_id": "scan-run", "state": "completed", "complete": True}
    )
    monkeypatch.setattr(scan.__module__, "SCAN_POLL_INTERVAL_SECONDS", 0)

    result = await scan(client, {}, "scan-request")

    assert result["complete"] is True
    assert client.get_calls == [path]
