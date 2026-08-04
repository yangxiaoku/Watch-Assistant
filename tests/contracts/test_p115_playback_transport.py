import pytest

from watch_assistant.adapters.p115_playback_transport import (
    P115_PLAYBACK_USER_AGENT,
    P115FixedPlaybackTransport,
)


class _Url:
    def __init__(self):
        self.url = "https://cdn.example.test/file"
        self.headers = {"user-agent": P115_PLAYBACK_USER_AGENT}

    def __str__(self):
        return self.url


class _Client:
    def __init__(self):
        self.pickcodes = []
        self.user_agents = []

    def download_url(self, pickcode, *, user_agent=None, **kwargs):
        del kwargs
        self.pickcodes.append(pickcode)
        self.user_agents.append(user_agent)
        return _Url()


@pytest.mark.asyncio
async def test_playback_transport_requests_and_forwards_verified_user_agent():
    client = _Client()

    def executor(method, payload, *, timeout_seconds):
        return method(payload, timeout_seconds=timeout_seconds)

    transport = P115FixedPlaybackTransport(client, call_executor=executor)
    link = await transport.download_url("SYNTHETIC_PROTECTED_VALUE", timeout_seconds=2)

    assert client.pickcodes == ["SYNTHETIC_PROTECTED_VALUE"]
    assert client.user_agents == [P115_PLAYBACK_USER_AGENT]
    assert link.request_headers == (("User-Agent", P115_PLAYBACK_USER_AGENT),)
