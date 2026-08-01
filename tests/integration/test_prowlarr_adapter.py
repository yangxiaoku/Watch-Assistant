import base64
import secrets
from urllib.parse import unquote

import httpx
import pytest
import respx

from watch_assistant.adapters.prowlarr import (
    ProwlarrAuthError,
    ProwlarrClient,
    ProwlarrInvalidResponseError,
)


@pytest.mark.integration
@respx.mock
async def test_prowlarr_search_uses_read_only_contract_and_filters_nzb():
    api_key = secrets.token_urlsafe(24)
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    route = respx.get(
        "http://prowlarr.test/api/v1/search",
        params={
            "query": "Inception 2010",
            "type": "search",
            "limit": "100",
            "offset": "0",
        },
    ).mock(
        return_value=httpx.Response(
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
        )
    )
    client = ProwlarrClient("http://prowlarr.test", api_key)

    result = await client.search("Inception 2010")
    await client.aclose()

    assert route.called
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
