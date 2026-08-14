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
    # 结构化 405 回退后为空页 → legacy 模式,还需一次文件索引请求(同样回退)。
    assert transport.fs_files_app_calls == 2
    assert transport.fs_files_calls == 2


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
