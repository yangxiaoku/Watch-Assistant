"""Offline Prowlarr API and Watch Assistant integration contracts.

The first group tests the local HTTP fixture and the official Prowlarr wire
shape. The strict-xfail group is intentionally red until the product adapter
and multi-source aggregator are connected on the implementation branch.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from tests.contract_support.prowlarr_mock import (
    MockResponse,
    ProwlarrMock,
    load_fixture,
)
from watch_assistant.services.api_errors import build_error_payload

TORRENT_INFOHASH = "0123456789abcdef0123456789abcdef01234567"
PENDING_TARGET = pytest.mark.xfail(
    strict=True,
    reason=(
        "待主实现接入 watch_assistant.adapters.prowlarr 与 "
        "watch_assistant.services.search_aggregation"
    ),
)


def _mapping_value(item: object, name: str) -> Any:
    if isinstance(item, Mapping):
        return item[name]
    return getattr(item, name)


async def test_mock_requires_header_auth_and_redacts_credential_from_capture():
    mock = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )

    async with mock.client() as client:
        denied = await client.get("/api/v1/search")
        query_denied = await client.get(
            "/api/v1/search",
            params={"apikey": mock.api_key},
        )
        accepted = await client.get(
            "/api/v1/search",
            headers=mock.auth_headers,
        )

    assert denied.status_code == 401
    assert query_denied.status_code == 401
    assert accepted.status_code == 200
    assert mock.requests[0].api_key_header_present is False
    assert mock.requests[0].api_key_header_valid is False
    assert mock.requests[1].api_key_in_query is True
    assert mock.requests[1].values("apikey") == ("<redacted>",)
    assert mock.requests[1].api_key_header_valid is False
    assert mock.requests[2].api_key_header_valid is True
    assert mock.requests[2].api_key_in_query is False
    assert mock.requests[2].authorization_header_present is False
    assert mock.api_key not in repr(mock)
    assert mock.api_key not in repr(mock.requests)


async def test_mock_reproduces_upstream_timeout_without_opening_a_listener():
    mock = ProwlarrMock([MockResponse.upstream_timeout()])

    async with mock.client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get("/api/v1/search", headers=mock.auth_headers)

    assert len(mock.requests) == 1
    assert mock.requests[0].api_key_header_valid is True


@pytest.mark.parametrize("status_code", [429, 500])
async def test_mock_reproduces_upstream_error_status_without_exposing_response_data(
    status_code: int,
):
    mock = ProwlarrMock(
        [MockResponse.failure(status_code, {"error": "fixture_failure"})]
    )

    async with mock.client() as client:
        response = await client.get("/api/v1/search", headers=mock.auth_headers)

    assert response.status_code == status_code
    assert response.json() == {"error": "fixture_failure"}


async def test_mock_preserves_official_search_parameters_and_repeated_arrays():
    mock = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )
    params = [
        ("query", "Example Show"),
        ("type", "tvsearch"),
        ("indexerIds", "101"),
        ("indexerIds", "202"),
        ("categories", "5000"),
        ("categories", "5070"),
        ("limit", "2"),
        ("offset", "0"),
    ]

    async with mock.client() as client:
        response = await client.get(
            "/api/v1/search",
            params=params,
            headers=mock.auth_headers,
        )

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    request = mock.requests[0]
    assert request.path == "/api/v1/search"
    assert request.values("query") == ("Example Show",)
    assert request.values("type") == ("tvsearch",)
    assert request.values("indexerIds") == ("101", "202")
    assert request.values("categories") == ("5000", "5070")
    assert request.values("limit") == ("2",)
    assert request.values("offset") == ("0",)
    assert request.api_key_in_query is False


def test_fixture_uses_official_protocol_enum_and_keeps_nzb_without_torrent_identity():
    page = load_fixture("search_page_1.json")
    assert {item["protocol"] for item in page} == {"torrent", "usenet"}

    torrent = next(item for item in page if item["protocol"] == "torrent")
    usenet = next(item for item in page if item["protocol"] == "usenet")
    assert torrent["infoHash"] == TORRENT_INFOHASH.upper()
    assert usenet["infoHash"] is None
    assert torrent["magnetUrl"] is None
    assert usenet["magnetUrl"] is None


async def test_mock_pagination_boundary_is_a_bare_array_and_has_no_implicit_cursor():
    mock = ProwlarrMock(
        [
            MockResponse.json(load_fixture("search_page_1.json")),
            MockResponse.json(load_fixture("search_page_2.json")),
            MockResponse.json(load_fixture("search_page_empty.json")),
        ]
    )

    async with mock.client() as client:
        first = await client.get(
            "/api/v1/search",
            params={"query": "Example Show", "limit": 2, "offset": 0},
            headers=mock.auth_headers,
        )
        second = await client.get(
            "/api/v1/search",
            params={"query": "Example Show", "limit": 2, "offset": 2},
            headers=mock.auth_headers,
        )
        terminal = await client.get(
            "/api/v1/search",
            params={"query": "Example Show", "limit": 2, "offset": 3},
            headers=mock.auth_headers,
        )

    assert len(first.json()) == 2
    assert len(second.json()) == 1
    assert terminal.json() == []
    assert mock.requests[0].values("offset") == ("0",)
    assert mock.requests[1].values("offset") == ("2",)
    assert mock.requests[2].values("offset") == ("3",)
    assert all("next_cursor" not in response for response in (first.json(), second.json()))


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (408, "resource_search_timeout"),
        (401, "resource_search_unavailable"),
        (429, "resource_search_unavailable"),
        (500, "resource_search_failed"),
    ],
)
def test_prowlarr_failures_map_to_existing_chinese_search_error_descriptors(
    status_code: int,
    error_code: str,
):
    payload = build_error_payload(
        error_code,
        status_code,
        request_id="request-contract",
        correlation_id="correlation-contract",
    )

    assert payload["code"] == error_code
    assert any("\u4e00" <= character <= "\u9fff" for character in payload["title_zh"])
    assert any("\u4e00" <= character <= "\u9fff" for character in payload["message_zh"])
    assert any("\u4e00" <= character <= "\u9fff" for character in payload["suggestion_zh"])


def _new_target_adapter(mock: ProwlarrMock) -> Any:
    try:
        module = importlib.import_module("watch_assistant.adapters.prowlarr")
    except ModuleNotFoundError as exc:
        raise AssertionError("Prowlarr adapter is not connected yet") from exc
    factory = getattr(module, "ProwlarrClient", None)
    if factory is None:
        raise AssertionError("ProwlarrClient target interface is not connected yet")
    return factory(
        base_url=mock.base_url,
        api_key=mock.api_key,
        timeout_seconds=1,
        transport=mock.transport(),
    )


def _new_target_aggregator(sources: list[Any]) -> Any:
    try:
        module = importlib.import_module("watch_assistant.services.search_aggregation")
    except ModuleNotFoundError as exc:
        raise AssertionError("multi-source aggregator is not connected yet") from exc
    factory = getattr(module, "aggregate_sources", None)
    if factory is None:
        raise AssertionError("aggregate_sources target interface is not connected yet")
    return factory(sources)


@PENDING_TARGET
async def test_target_adapter_uses_header_only_and_passes_official_search_query():
    mock = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )
    adapter = _new_target_adapter(mock)

    await adapter.search(
        "Example Show",
        search_type="tvsearch",
        indexer_ids=[101, 202],
        categories=[5000, 5070],
        limit=2,
        offset=0,
    )

    request = mock.requests[0]
    assert request.api_key_header_valid is True
    assert request.api_key_in_query is False
    assert request.authorization_header_present is False
    assert request.values("query") == ("Example Show",)
    assert request.values("type") == ("tvsearch",)


@PENDING_TARGET
async def test_target_adapter_distinguishes_torrent_and_nzb_and_normalizes_infohash():
    mock = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )
    adapter = _new_target_adapter(mock)

    results = await adapter.search("Example Show", search_type="tvsearch", limit=2, offset=0)
    torrent = next(item for item in results if _mapping_value(item, "protocol") == "torrent")
    nzb = next(item for item in results if _mapping_value(item, "protocol") in {"nzb", "usenet"})

    assert _mapping_value(torrent, "infohash") == TORRENT_INFOHASH
    assert _mapping_value(nzb, "infohash") is None
    assert _mapping_value(torrent, "kind") == "torrent"
    assert _mapping_value(nzb, "kind") == "nzb"


@PENDING_TARGET
async def test_target_aggregator_merges_duplicate_infohash_and_keeps_observations():
    first = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )
    second = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_2.json"))]
    )
    aggregator = _new_target_aggregator(
        [_new_target_adapter(first), _new_target_adapter(second)]
    )

    result = await aggregator.search("Example Show", search_type="tvsearch")
    matches = [
        item
        for item in result.results
        if _mapping_value(item, "infohash") == TORRENT_INFOHASH
    ]

    assert len(matches) == 1
    observations = _mapping_value(matches[0], "observations")
    assert {observation["source_id"] for observation in observations} == {
        "fixture-torrent-one",
        "fixture-torrent-two",
    }


@pytest.mark.parametrize(
    "failure",
    [MockResponse.upstream_timeout(), MockResponse.failure(500, {"error": "fixture_failure"})],
)
@PENDING_TARGET
async def test_target_aggregator_keeps_healthy_source_on_timeout_or_error(
    failure: MockResponse,
):
    healthy = ProwlarrMock(
        [MockResponse.json(load_fixture("search_page_1.json"))]
    )
    failing = ProwlarrMock([failure])
    aggregator = _new_target_aggregator(
        [_new_target_adapter(healthy), _new_target_adapter(failing)]
    )

    result = await aggregator.search("Example Show", search_type="tvsearch")

    assert result.results
    assert any(warning.code == "partial_upstream" for warning in result.warnings)
    assert result.results[0].source_id == "fixture-torrent-one"


@PENDING_TARGET
async def test_target_aggregator_stops_pagination_after_short_page():
    mock = ProwlarrMock(
        [
            MockResponse.json(load_fixture("search_page_1.json")),
            MockResponse.json(load_fixture("search_page_2.json")),
        ]
    )
    adapter = _new_target_adapter(mock)
    aggregator = _new_target_aggregator([adapter])

    await aggregator.search("Example Show", search_type="tvsearch", page_size=2)

    assert [request.values("offset") for request in mock.requests] == [
        ("0",),
        ("2",),
    ]


@PENDING_TARGET
async def test_target_maps_prowlarr_upstream_failure_to_chinese_error_code():
    mock = ProwlarrMock([MockResponse.failure(429, {"error": "fixture_rate_limited"})])
    aggregator = _new_target_aggregator([_new_target_adapter(mock)])

    result = await aggregator.search("Example Show", search_type="tvsearch")

    assert result.error_code == "resource_search_unavailable"
    assert "\u4e00" in result.message_zh
