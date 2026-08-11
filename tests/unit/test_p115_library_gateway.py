import pytest

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)


class _FakeTransport:
    def __init__(self, response):
        self._response = response

    async def fs_files_app(self, payload, *, timeout_seconds):
        raise RuntimeError("app endpoint unavailable")

    async def fs_files(self, payload, *, timeout_seconds):
        return self._response

    async def fs_info_app(self, payload, *, timeout_seconds):
        raise RuntimeError("app endpoint unavailable")

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
