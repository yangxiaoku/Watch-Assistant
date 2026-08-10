from __future__ import annotations

import base64
import secrets
from urllib.parse import unquote

import httpx
import pytest
import respx

from watch_assistant.adapters.prowlarr import (
    _MAX_RESPONSE_BYTES,
    ProwlarrAuthError,
    ProwlarrClient,
    ProwlarrError,
    ProwlarrInvalidResponseError,
    _PinnedAsyncHTTPTransport,
)
from watch_assistant.services.prowlarr_settings import _normalize_base_url_details
from watch_assistant.services.source_health import SourceHealthTracker


class _RecordingStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.sni_hostname: str | None = None
        self._response_sent = False

    async def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
        if self._response_sent:
            return b""
        self._response_sent = True
        return (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n[]"
        )

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _RecordingStream:
        del ssl_context
        self.sni_hostname = server_hostname
        return self

    def get_extra_info(self, _info: str):
        return None


class _RecordingBackend:
    def __init__(self, stream: _RecordingStream) -> None:
        self.stream = stream
        self.connected_host: str | None = None

    async def connect_tcp(self, host: str, port: int, **_kwargs):
        self.connected_host = host
        return self.stream

    async def connect_unix_socket(self, path: str, **_kwargs):
        raise AssertionError(f"unexpected unix socket: {path}")

    async def sleep(self, _seconds: float):
        return None


@pytest.mark.integration
async def test_pinned_transport_reuses_validated_address_and_preserves_hostname():
    validated_url, validated_address = _normalize_base_url_details(
        "https://prowlarr.test",
        hostname_resolver=lambda _hostname: ("93.184.216.34",),
    )
    assert validated_url == "https://prowlarr.test"
    assert validated_address == "93.184.216.34"

    stream = _RecordingStream()
    backend = _RecordingBackend(stream)
    transport = _PinnedAsyncHTTPTransport(validated_address, backend=backend)
    async with httpx.AsyncClient(
        base_url=validated_url,
        transport=transport,
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/v1/search")

    assert response.status_code == 200
    assert backend.connected_host == validated_address
    assert stream.sni_hostname == "prowlarr.test"
    assert b"host: prowlarr.test" in b"".join(stream.writes).lower()


class _MockProwlarrClient(ProwlarrClient):
    def __init__(self, respx_mock, base_url: str, api_key: str, **kwargs):
        self._injected_client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            transport=httpx.MockTransport(respx_mock.async_handler),
        )
        super().__init__(base_url, api_key, client=self._injected_client, **kwargs)

    async def aclose(self) -> None:
        await self._injected_client.aclose()


def _mocked_client(respx_mock, base_url: str, api_key: str, **kwargs):
    return _MockProwlarrClient(respx_mock, base_url, api_key, **kwargs)


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_search_uses_read_only_contract_and_filters_nzb(respx_mock):
    api_key = secrets.token_urlsafe(24)
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    # L5:默认每页 25 条;首页必须是满页(25 条)才会继续翻页,
    # 短页(不足页大小)即视为最后一页,直接结束。
    first_page = [
        {
            "guid": f"release-{index}",
            "title": f"Inception 2010 1080p release {index}",
            "protocol": "Torrent",
            "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            "infoHash": info_hash,
            "size": 1234,
            "seeders": 8,
            "indexer": "Indexer A",
            "indexerId": 7,
            "publishDate": "2026-08-01T00:00:00Z",
        }
        for index in range(1, 25)
    ]
    first_page.append(
        {
            "title": "Inception NZB",
            "protocol": "Usenet",
            "downloadUrl": "https://example.test/inception.nzb",
        }
    )
    route = respx_mock.get(path="/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=first_page),
            httpx.Response(200, json=[]),
        ]
    )
    client = _mocked_client(respx_mock, "http://prowlarr.test", api_key)

    result = await client.search("Inception 2010")
    await client.aclose()

    assert route.called
    assert route.call_count == 2
    assert route.calls[0].request.url.params["limit"] == "25"
    assert route.calls[0].request.url.params["offset"] == "0"
    assert route.calls[1].request.url.params["limit"] == "25"
    assert route.calls[1].request.url.params["offset"] == "25"
    assert route.calls[0].request.headers["X-Api-Key"] == api_key
    assert api_key not in str(route.calls[0].request.url)
    assert result.unsupported_count == 1
    assert len(result.releases) == 24
    assert result.releases[0].info_hash == info_hash
    assert result.releases[0].indexer_id == 7


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_builds_magnet_from_verified_infohash_when_needed(respx_mock):
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    base32_hash = base64.b32encode(bytes.fromhex(info_hash)).decode().rstrip("=")
    respx_mock.get(path="/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "title": "Movie",
                    "protocol": "torrent",
                    "infoHash": base32_hash,
                }
            ],
        )
    )
    client = _mocked_client(
        respx_mock, "http://prowlarr.test", secrets.token_urlsafe(24)
    )

    result = await client.search("Movie")
    await client.aclose()

    assert result.releases[0].info_hash == info_hash
    assert f"urn:btih:{info_hash}" in unquote(result.releases[0].magnet_url)


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_auth_error_does_not_expose_api_key(respx_mock):
    api_key = secrets.token_urlsafe(24)
    respx_mock.get(path="/api/v1/search").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    client = _mocked_client(respx_mock, "http://prowlarr.test", api_key)

    with pytest.raises(ProwlarrAuthError) as error:
        await client.search("Movie")
    await client.aclose()

    assert str(error.value) == "Prowlarr authentication failed"
    assert api_key not in str(error.value)


@pytest.mark.integration
async def test_prowlarr_does_not_follow_search_redirects():
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host or "")
        if len(requested_hosts) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://redirect-target.test/api/v1/search"},
                request=request,
            )
        return httpx.Response(
            200,
            json=[],
            request=request,
        )

    transport_client = httpx.AsyncClient(
        base_url="https://prowlarr.test",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    client = ProwlarrClient(
        "https://prowlarr.test", "fixture-only", client=transport_client
    )

    try:
        with pytest.raises(ProwlarrError):
            await client.search("Movie")
    finally:
        await client.aclose()
        await transport_client.aclose()

    assert requested_hosts == ["prowlarr.test"]


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_rejects_zero_infohash(respx_mock):
    respx_mock.get(path="/api/v1/search").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "title": "Invalid release",
                        "protocol": "torrent",
                        "infoHash": "0" * 40,
                    }
                ],
            ),
            httpx.Response(200, json=[]),
        ]
    )
    client = _mocked_client(respx_mock, "http://prowlarr.test", "fixture-only")

    try:
        result = await client.search("Movie")
    finally:
        await client.aclose()

    assert result.releases == ()
    assert result.unsupported_count == 1


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_rejects_non_array_response_without_returning_body(respx_mock):
    api_key = secrets.token_urlsafe(24)
    respx_mock.get(path="/api/v1/search").mock(
        return_value=httpx.Response(200, json={"error": "unexpected"})
    )
    client = _mocked_client(respx_mock, "http://prowlarr.test", api_key)

    with pytest.raises(ProwlarrInvalidResponseError) as error:
        await client.search("Movie")
    await client.aclose()

    assert str(error.value) == "Unexpected Prowlarr response shape"
    assert "unexpected" not in str(error.value)


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_invalid_indexer_ids_never_freeze_the_circuit_probe(respx_mock):
    # 回归(S2):参数构建必须发生在 allow_request() 之前。若在探针放行
    # (_probe_in_flight=True)后抛 ValueError(非 int 索引器 id),
    # 既无 record_failure 也无 release_request,探针永真会把来源
    # 永久冻结在 BACKOFF;参数校验必须先于熔断器放行。
    api_key = secrets.token_urlsafe(24)
    route = respx_mock.get(path="/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = _mocked_client(
        respx_mock,
        "http://prowlarr.test",
        api_key,
        health_tracker=SourceHealthTracker(failure_threshold=10),
    )
    try:
        with pytest.raises(ValueError):
            await client.search("Movie", indexer_ids=(11, "not-an-int"))

        # 参数错误被拒绝后,探针从未放行,熔断器仍可正常放行后续请求。
        assert client._health.allow_request() is True
        result = await client.search("Movie")
        assert result is not None
    finally:
        await client.aclose()
    assert route.call_count == 1


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_rejects_unbounded_or_non_json_success_response(respx_mock):
    route = respx_mock.get(path="/api/v1/search").mock(
        side_effect=[
            httpx.Response(
                200,
                content=b"[]",
                headers={"content-type": "text/plain"},
            ),
            httpx.Response(
                200,
                content=b"x" * (_MAX_RESPONSE_BYTES + 1),
                headers={"content-type": "application/json"},
            ),
            httpx.Response(
                200,
                content=b"[" * 13 + b"0" + b"]" * 13,
                headers={"content-type": "application/json"},
            ),
            httpx.Response(
                200,
                content=b'[{"title":"' + b"x" * 8193 + b'"}]',
                headers={"content-type": "application/json"},
            ),
        ]
    )
    client = _mocked_client(
        respx_mock,
        "http://prowlarr.test",
        "fixture-only",
        health_tracker=SourceHealthTracker(failure_threshold=10),
    )

    try:
        for _ in range(4):
            with pytest.raises(ProwlarrInvalidResponseError) as error:
                await client.search("Movie")
            assert error.value.__cause__ is None
    finally:
        await client.aclose()

    assert route.call_count == 4


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_preserves_base_url_path_prefix(respx_mock):
    route = respx_mock.get(path="/prowlarr/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = _mocked_client(
        respx_mock,
        "http://prowlarr.test/prowlarr", secrets.token_urlsafe(24)
    )

    await client.search("Movie")
    await client.aclose()

    assert route.called


@pytest.mark.integration
@respx.mock(using="httpx")
async def test_prowlarr_accepts_bounded_upstream_overfetch(respx_mock):
    first_page = [
        {
            "title": f"Release {index}",
            "protocol": "torrent",
            "infoHash": f"{index:040x}",
        }
        for index in range(1, 31)
    ]
    second_page = [
        {
            "title": f"Release {index}",
            "protocol": "torrent",
            "infoHash": f"{index:040x}",
        }
        for index in range(31, 51)
    ]
    route = respx_mock.get(path="/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=first_page),
            httpx.Response(200, json=second_page),
        ]
    )
    client = _mocked_client(
        respx_mock, "http://prowlarr.test", secrets.token_urlsafe(24)
    )

    try:
        result = await client.search("Movie", limit=50, page_size=1)
    finally:
        await client.aclose()

    assert route.call_count == 2
    assert [call.request.url.params["limit"] for call in route.calls] == ["1", "1"]
    assert [call.request.url.params["offset"] for call in route.calls] == ["0", "30"]
    assert result.truncated is False
    assert len(result.releases) == 50
    assert result.releases[0].info_hash == f"{1:040x}"


@pytest.mark.integration
async def test_download_resolution_rejects_cross_host_and_internal_ports(respx_mock):
    """H2:downloadUrl/重定向 Location 只能指向 Prowlarr 端点自身的 host:port。

    恶意索引器返回 http://127.0.0.1:6379 等地址时,请求会携带 X-Api-Key
    打到 Prowlarr 主机内部端口(受限 SSRF + API Key 泄漏),必须直接跳过。
    """
    client = _mocked_client(
        respx_mock, "https://prowlarr.test:9696", secrets.token_urlsafe(24)
    )
    assert client._allowed_download_target("https://prowlarr.test:9696/dl/1") is True
    # 端点端口 9696:无端口 URL(默认 443)同样拒绝
    assert client._allowed_download_target("https://prowlarr.test/dl/1") is False
    # 跨 host / 内部端口 / 任意端口一律拒绝
    assert client._allowed_download_target("http://127.0.0.1:9696/dl/1") is False
    assert client._allowed_download_target("https://prowlarr.test:6379/dl/1") is False
    assert client._allowed_download_target("http://evil.test/dl/1") is False
    assert client._allowed_download_target("http://prowlarr.test:80/dl/1") is False
    assert client._allowed_download_target("not a url") is False

    # 解析路径同样拒绝恶意目标,不发起任何请求
    assert await client._resolve_download_url("http://127.0.0.1:6379/keys") is None
    assert await client._follow_redirect_magnet("http://prowlarr.test:6379/keys") is None
    await client.aclose()


@pytest.mark.integration
async def test_download_redirect_to_cross_host_is_rejected(respx_mock):
    """第一跳合法、Location 指向他处时不得跟随。"""
    client = _mocked_client(
        respx_mock, "http://prowlarr.test:9696", secrets.token_urlsafe(24)
    )
    assert client._allowed_download_target("http://prowlarr.test:9696/dl/1") is True
    # 重定向 Location 跨 host/端口 → 拒绝
    assert client._allowed_download_target("http://evil.test/magnet") is False
    assert client._allowed_download_target("http://prowlarr.test:6379/x") is False
    await client.aclose()
