import sys
from types import SimpleNamespace

import pytest

from watch_assistant.adapters.p115_library_transport import (
    P115FixedReadOnlyTransport,
    P115ReadOnlyTransportUnavailable,
    create_p115_readonly_transport,
    p115_readonly_timeout_executor,
)


class _Client:
    def __init__(self):
        self.calls = []

    def fs_files(self, payload, **kwargs):
        self.calls.append(("fs_files", dict(payload), kwargs))
        return {"state": True, "data": [], "offset": 0, "limit": 1, "count": 0}

    def fs_info(self, payload, **kwargs):
        self.calls.append(("fs_info", dict(payload), kwargs))
        return {"state": True}

    def fs_files_app(self, payload, **kwargs):
        self.calls.append(("fs_files_app", dict(payload), kwargs))
        return {"state": True, "data": [], "offset": 0, "limit": 1, "count": 0}

    def fs_info_app(self, payload, **kwargs):
        self.calls.append(("fs_info_app", dict(payload), kwargs))
        return {"state": True}


@pytest.mark.asyncio
async def test_transport_calls_only_allowlisted_methods_with_injected_timeout():
    client = _Client()
    executor_calls = []

    def executor(method, payload, *, timeout_seconds):
        executor_calls.append((method.__name__, dict(payload), timeout_seconds))
        return method(payload)

    transport = P115FixedReadOnlyTransport(client, call_executor=executor)

    await transport.fs_files({"cid": "7", "limit": 1, "offset": 0}, timeout_seconds=4)
    await transport.fs_info({"file_id": "8"}, timeout_seconds=3)

    assert executor_calls == [
        ("fs_files", {"cid": "7", "limit": 1, "offset": 0}, 4.0),
        ("fs_info", {"file_id": "8"}, 3.0),
    ]
    assert [call[0] for call in client.calls] == ["fs_files", "fs_info"]


@pytest.mark.asyncio
async def test_invalid_timeout_rejects_before_any_client_call():
    client = _Client()
    transport = P115FixedReadOnlyTransport(
        client, call_executor=lambda *_args, **_kwargs: None
    )

    with pytest.raises(P115ReadOnlyTransportUnavailable, match="blocked_environment"):
        await transport.fs_files({"cid": "7"}, timeout_seconds=0)

    assert client.calls == []


@pytest.mark.asyncio
async def test_405_uses_documented_app_read_endpoints_only():
    client = _Client()
    executor_calls = []

    def executor(method, payload, *, timeout_seconds):
        executor_calls.append(method.__name__)
        if method.__name__ in {"fs_files", "fs_info"}:
            error = RuntimeError("provider response")
            error.status = 405  # type: ignore[attr-defined]
            raise error
        return method(payload)

    transport = P115FixedReadOnlyTransport(client, call_executor=executor)

    await transport.fs_files({"cid": "7"}, timeout_seconds=4)
    await transport.fs_info({"fid": "8"}, timeout_seconds=4)

    assert executor_calls == ["fs_files", "fs_files_app", "fs_info", "fs_info_app"]


@pytest.mark.asyncio
async def test_non_405_failure_is_not_hidden_by_app_fallback():
    client = _Client()

    def executor(method, payload, *, timeout_seconds):
        if method.__name__ == "fs_files":
            error = RuntimeError("provider response")
            error.status = 503  # type: ignore[attr-defined]
            raise error
        return method(payload)

    transport = P115FixedReadOnlyTransport(client, call_executor=executor)

    with pytest.raises(RuntimeError):
        await transport.fs_files({"cid": "7"}, timeout_seconds=4)
    assert [call[0] for call in client.calls] == []


def test_native_executor_forces_timeout_and_disables_retries(monkeypatch):
    received = {}

    def request(*, async_, **kwargs):
        received["async"] = async_
        received.update(kwargs)
        return {"state": True}

    monkeypatch.setitem(
        sys.modules, "urllib3_future_request", SimpleNamespace(request=request)
    )

    def method(payload, *, async_, request):
        assert payload == {"cid": "7"}
        return request(async_=async_, url="https://example.invalid", headers={})

    assert p115_readonly_timeout_executor(
        method, {"cid": "7"}, timeout_seconds=2.5
    ) == {"state": True}
    assert received["async"] is False
    assert received["timeout"] == 2.5
    assert received["retries"] is False


def test_factory_rejects_version_mismatch_before_client_import(monkeypatch):
    monkeypatch.setattr(
        "watch_assistant.adapters.p115_library_transport._p115client_version",
        lambda: "mismatch",
    )
    monkeypatch.delitem(sys.modules, "p115client", raising=False)

    with pytest.raises(P115ReadOnlyTransportUnavailable, match="blocked_environment"):
        create_p115_readonly_transport("SYNTHETIC_COOKIE_SECRET")
