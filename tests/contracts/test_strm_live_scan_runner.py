import json
import time
from types import SimpleNamespace

import pytest

import scripts.p115_strm_application_live_runner as application_runner
import scripts.p115_strm_playback_live_runner as playback_runner
from scripts.p115_strm_application_live_runner import _post_cleanup
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
    ("scan", "runner", "path"),
    (
        (
            application_scan,
            application_runner,
            "/api/v1/libraries/strm-live/scans/scan-run",
        ),
        (
            playback_scan,
            playback_runner,
            "/api/v1/libraries/strm-playback-live/scans/scan-run",
        ),
    ),
)
async def test_strm_live_scan_waits_for_queued_operation(
    scan, runner, path, monkeypatch
):
    client = _Client(
        {"run_id": "scan-run", "state": "completed", "complete": True}
    )
    monkeypatch.setattr(runner, "SCAN_POLL_INTERVAL_SECONDS", 0)

    result = await scan(client, {}, "scan-request")

    assert result["complete"] is True
    assert client.get_calls == [path]


@pytest.mark.asyncio
async def test_strm_live_rename_passes_explicit_write_confirmation(tmp_path, monkeypatch):
    authorization = tmp_path / "rename-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "version": 1,
                "parent_id": "7",
                "expires_at": time.time() + 60,
                "nonce": "rename-test",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class _Transport:
        receipts = ("receipt",)

        def __init__(self):
            self.name = "wa-live-probe.mp4"

        async def read_object(self, _file_id):
            return SimpleNamespace(name=self.name)

        async def read_target(self, _parent_id, _name):
            return None

        async def rename(self, _file_id, _name):
            self.name = _name
            return SimpleNamespace(status=SimpleNamespace(value="success"))

    def factory(**kwargs):
        captured.update(kwargs)
        return _Transport()

    monkeypatch.setattr(
        application_runner,
        "create_live_p115_organization_transport",
        factory,
    )

    result = await application_runner._rename_remote(
        object(),
        "7",
        "8",
        authorization,
        "wa-live-probe.mp4",
        "wa-strm-probe-renamed.mp4",
    )

    assert result == {"status": "success", "receipt_count": 1}
    assert captured["write_enabled"] is True
    assert captured["plan_confirmed"] is True


@pytest.mark.asyncio
async def test_strm_live_cleanup_applies_reviewed_plan():
    class _CleanupClient:
        def __init__(self):
            self.calls = []

        async def post(self, path, *, headers, json):
            self.calls.append((path, headers, json))
            if path.endswith("strm-cleanup-plan"):
                return _Response(
                    {
                        "plan_id": "cleanup-plan",
                        "revision": 1,
                        "plan_hash": "a" * 64,
                    }
                )
            return _Response({"retired": 0, "plan": {"status": "applied"}})

    client = _CleanupClient()
    result = await _post_cleanup(client, {"X-CSRF-Token": "csrf"}, "scan-run")

    assert result["retired"] == 0
    assert client.calls[0][0] == "/api/v1/libraries/strm-live/strm-cleanup-plan"
    assert client.calls[1][0] == "/api/v1/strm-cleanup-plans/cleanup-plan/apply"
    assert client.calls[1][2] == {
        "expected_revision": 1,
        "digest": "a" * 64,
        "confirm": True,
        "idempotency_key": "strm-live-cleanup-confirmed",
    }
