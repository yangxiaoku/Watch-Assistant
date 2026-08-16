import pytest

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)


class _FakeTransport:
    def __init__(self, response):
        self._response = response

    async def fs_files_app(self, payload, *, timeout_seconds):
        from urllib.error import HTTPError

        raise HTTPError(
            "https://proapi.115.com/android/ufile/files",
            405,
            "Method Not Allowed",
            None,
            None,
        )

    async def fs_files(self, payload, *, timeout_seconds):
        return self._response

    async def fs_info_app(self, payload, *, timeout_seconds):
        from urllib.error import HTTPError

        raise HTTPError(
            "https://proapi.115.com/android/ufile/info",
            405,
            "Method Not Allowed",
            None,
            None,
        )

    async def fs_info(self, payload, *, timeout_seconds):
        return self._response


class _NotifyingProvider:
    def __init__(self, cookie="credential-marker"):
        self.cookie = cookie
        self.failures = 0

    def load(self):
        return self.cookie

    def notify_failure(self):
        self.failures += 1


class _PlainProvider:
    def __init__(self, cookie="credential-marker"):
        self.cookie = cookie

    def load(self):
        return self.cookie


def _gateway(provider, response):
    return P115ReadOnlyDirectoryGateway(
        provider,
        transport_factory=lambda credential: _FakeTransport(response),
        authorized_directory_ids=("7000",),
    )


@pytest.mark.asyncio
async def test_gateway_notifies_provider_on_login_invalid_errno():
    provider = _NotifyingProvider()
    gateway = _gateway(provider, {"errno": 990001, "data": None})

    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await gateway._call("fs_files", {"cid": "7000"}, deadline=gateway._deadline())
    assert provider.failures == 1


@pytest.mark.asyncio
async def test_gateway_notifies_provider_on_string_auth_errno():
    provider = _NotifyingProvider()
    gateway = _gateway(provider, {"errno": "990001", "data": None})

    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await gateway._call("fs_info", {"cid": "7000"}, deadline=gateway._deadline())
    assert provider.failures == 1


@pytest.mark.asyncio
async def test_gateway_does_not_notify_on_business_failure():
    provider = _NotifyingProvider()
    gateway = _gateway(provider, {"errno": 990005, "data": None})

    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await gateway._call("fs_files", {"cid": "7000"}, deadline=gateway._deadline())
    assert provider.failures == 0


class _Structured405Transport:
    """app 接口返回含 status_code=405 的 Mapping(不抛异常),旧接口正常。"""

    def __init__(self, response):
        self._response = response
        self.fs_files_calls = 0
        self.fs_files_app_calls = 0

    async def fs_files_app(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        self.fs_files_app_calls += 1
        return {"state": True, "status_code": 405, "data": None}

    async def fs_files(self, payload, *, timeout_seconds):
        del timeout_seconds
        self.fs_files_calls += 1
        return self._response

    async def fs_info_app(self, payload, *, timeout_seconds):
        del payload, timeout_seconds
        return {"state": True, "status_code": 405, "data": None}

    async def fs_info(self, payload, *, timeout_seconds):
        del timeout_seconds
        return self._response


@pytest.mark.asyncio
async def test_gateway_falls_back_to_legacy_on_structured_405():
    """app 接口返回结构化 405(不抛异常)时同样回退旧接口,而不是被
    _response_success 误判为成功再在 _parse_page 报 pagination 错误。"""
    transport = _Structured405Transport(
        {"state": True, "data": [], "offset": 0, "limit": 1, "count": 0}
    )
    gateway = P115ReadOnlyDirectoryGateway(
        _PlainProvider(),
        transport_factory=lambda credential: transport,
        authorized_directory_ids=("7000",),
    )

    page = await gateway.list_directory("7000", page=1, page_size=1)

    assert page is not None
    assert page.items == ()
    assert transport.fs_files_app_calls == 1
    assert transport.fs_files_calls == 1


@pytest.mark.asyncio
async def test_gateway_tolerates_provider_without_notify_failure():
    provider = _PlainProvider()
    gateway = _gateway(provider, {"errno": 990001, "data": None})

    with pytest.raises(P115ReadOnlyGatewayError, match="remote_failed"):
        await gateway._call("fs_files", {"cid": "7000"}, deadline=gateway._deadline())


@pytest.mark.asyncio
async def test_gateway_does_not_notify_on_success():
    provider = _NotifyingProvider()
    gateway = _gateway(provider, {"errno": 0, "data": {}})

    response = await gateway._call(
        "fs_files", {"cid": "7000"}, deadline=gateway._deadline()
    )
    assert response["errno"] == 0
    assert provider.failures == 0


def test_throttle_read_spaces_real_requests(monkeypatch):
    """进程级节流:间隔不足最小间隔时 sleep 补齐,足够时直接放行。"""
    import sys

    module = sys.modules[
        "watch_assistant.adapters.p115_library_transport"
    ]
    clock = {"now": 1000.0}
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds))
    module._last_read_at = 0.0

    module.throttle_read()  # 距上次调用很远 → 不 sleep
    assert sleeps == []

    clock["now"] += 0.1
    module.throttle_read()  # 间隔 0.1s < 0.5s → sleep 补齐
    assert len(sleeps) == 1 and sleeps[0] > 0

    clock["now"] += 1.0
    module.throttle_read()  # 间隔 1.0s > 0.5s → 不 sleep
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_list_directory_drops_self_referencing_container_entries():
    """115 离线下载容器返回的自引用条目(id 等于父目录)被丢弃,真实条目保留。

    下载中的文件无 fid/pid,fs_info 判型为目录且 id 回退为容器目录自身,
    树扫描会因此检测到自环(directory_cycle)导致整个整理扫描失败。"""
    transport = _FakeTransport(
        {
            "state": True,
            "offset": 0,
            "limit": 50,
            "count": 3,
            "data": [
                {"is_dir": True, "cid": "7000", "n": "container-self-ref"},
                {"is_dir": False, "fid": "8001", "pid": "7000", "n": "real.mkv"},
                {"is_dir": True, "cid": "9000", "pid": "7000", "n": "real-dir"},
            ],
        }
    )
    gateway = P115ReadOnlyDirectoryGateway(
        _PlainProvider(),
        transport_factory=lambda credential: transport,
        authorized_directory_ids=("7000",),
    )

    page = await gateway.list_directory("7000", page=1, page_size=50)

    names = [entry.name for entry in page.items]
    assert names == ["real.mkv", "real-dir"]
    assert "9000" in gateway._observed_directories
    assert "7000" not in gateway._observed_directories


@pytest.mark.asyncio
async def test_list_directory_drops_self_referencing_file_entries():
    """文件条目 id 等于父目录时同样丢弃,防止污染 observed_files。"""
    transport = _FakeTransport(
        {
            "state": True,
            "offset": 0,
            "limit": 50,
            "count": 2,
            "data": [
                {"is_dir": False, "fid": "7000", "n": "self-ref-file"},
                {"is_dir": False, "fid": "8002", "pid": "7000", "n": "ok.bin"},
            ],
        }
    )
    gateway = P115ReadOnlyDirectoryGateway(
        _PlainProvider(),
        transport_factory=lambda credential: transport,
        authorized_directory_ids=("7000",),
    )

    page = await gateway.list_directory("7000", page=1, page_size=50)

    names = [entry.name for entry in page.items]
    assert names == ["ok.bin"]
    assert "7000" not in gateway._observed_files
    assert "8002" in gateway._observed_files
