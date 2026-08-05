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
)
from watch_assistant.services.source_health import SourceHealthTracker


@pytest.mark.integration
@respx.mock
async def test_prowlarr_search_uses_read_only_contract_and_filters_nzb():
    api_key = secrets.token_urlsafe(24)
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    route = respx.get("http://prowlarr.test/api/v1/search").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "guid": "release-1",
                        "title": "Inception 2010 1080p",
                        "protocol": "Torrent",
                        "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
                        "infoHash": info_hash,
                        "size": 1234,
                        "seeders": 8,
                        "indexer": "Indexer A",
                        "indexerId": 7,
                        "publishDate": "2026-08-01T00:00:00Z",
                    },
                    {
                        "title": "Inception NZB",
                        "protocol": "Usenet",
                        "downloadUrl": "https://example.test/inception.nzb",
                    },
                ],
            ),
            httpx.Response(200, json=[]),
        ]
    )
    client = ProwlarrClient("http://prowlarr.test", api_key)

    result = await client.search("Inception 2010")
    await client.aclose()

    assert route.called
    assert route.call_count == 2
    assert route.calls[0].request.url.params["limit"] == "1"
    assert route.calls[0].request.url.params["offset"] == "0"
    assert route.calls[1].request.url.params["limit"] == "1"
    assert route.calls[1].request.url.params["offset"] == "2"
    assert route.calls[0].request.headers["X-Api-Key"] == api_key
    assert api_key not in str(route.calls[0].request.url)
    assert result.unsupported_count == 1
    assert len(result.releases) == 1
    assert result.releases[0].info_hash == info_hash
    assert result.releases[0].indexer_id == 7


@pytest.mark.integration
@respx.mock
async def test_prowlarr_builds_magnet_from_verified_infohash_when_needed():
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    base32_hash = base64.b32encode(bytes.fromhex(info_hash)).decode().rstrip("=")
    respx.get("http://prowlarr.test/api/v1/search").mock(
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
    client = ProwlarrClient("http://prowlarr.test", secrets.token_urlsafe(24))

    result = await client.search("Movie")
    await client.aclose()

    assert result.releases[0].info_hash == info_hash
    assert f"urn:btih:{info_hash}" in unquote(result.releases[0].magnet_url)


@pytest.mark.integration
@respx.mock
async def test_prowlarr_auth_error_does_not_expose_api_key():
    api_key = secrets.token_urlsafe(24)
    respx.get("http://prowlarr.test/api/v1/search").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    client = ProwlarrClient("http://prowlarr.test", api_key)

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
@respx.mock
async def test_prowlarr_rejects_zero_infohash():
    respx.get("http://prowlarr.test/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "title": "Invalid release",
                    "protocol": "torrent",
                    "infoHash": "0" * 40,
                }
            ],
        )
    )
    client = ProwlarrClient("http://prowlarr.test", "fixture-only")

    try:
        result = await client.search("Movie")
    finally:
        await client.aclose()

    assert result.releases == ()
    assert result.unsupported_count == 1


@pytest.mark.integration
@respx.mock
async def test_prowlarr_rejects_non_array_response_without_returning_body():
    api_key = secrets.token_urlsafe(24)
    respx.get("http://prowlarr.test/api/v1/search").mock(
        return_value=httpx.Response(200, json={"error": "unexpected"})
    )
    client = ProwlarrClient("http://prowlarr.test", api_key)

    with pytest.raises(ProwlarrInvalidResponseError) as error:
        await client.search("Movie")
    await client.aclose()

    assert str(error.value) == "Unexpected Prowlarr response shape"
    assert "unexpected" not in str(error.value)


@pytest.mark.integration
@respx.mock
async def test_prowlarr_rejects_unbounded_or_non_json_success_response():
    route = respx.get("http://prowlarr.test/api/v1/search").mock(
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
    client = ProwlarrClient(
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
@respx.mock
async def test_prowlarr_preserves_base_url_path_prefix():
    route = respx.get("http://prowlarr.test/prowlarr/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = ProwlarrClient(
        "http://prowlarr.test/prowlarr", secrets.token_urlsafe(24)
    )

    await client.search("Movie")
    await client.aclose()

    assert route.called


@pytest.mark.integration
@respx.mock
async def test_prowlarr_accepts_bounded_upstream_overfetch():
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
    route = respx.get("http://prowlarr.test/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=first_page),
            httpx.Response(200, json=second_page),
        ]
    )
    client = ProwlarrClient("http://prowlarr.test", secrets.token_urlsafe(24))

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
