"""Offline Prowlarr API and Watch Assistant integration contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.contract_support.prowlarr_mock import (
    MockResponse,
    ProwlarrMock,
    load_fixture,
)
from watch_assistant.adapters.prowlarr import (
    ProwlarrCircuitOpenError,
    ProwlarrClient,
    ProwlarrRateLimitError,
    ProwlarrServerError,
    ProwlarrTimeoutError,
)
from watch_assistant.models import Resource
from watch_assistant.schemas import (
    ProwlarrSettingsResponse,
    ProwlarrVerifyResponse,
    SearchSourcesResponse,
)
from watch_assistant.services.api_errors import build_error_payload
from watch_assistant.services.search import (
    SearchService,
    SearchUnavailable,
    _resource_summary,
)

TORRENT_INFOHASH = "0123456789abcdef0123456789abcdef01234567"


def _mapping_value(item: object, name: str) -> Any:
    if isinstance(item, Mapping):
        return item[name]
    return getattr(item, name)


def _new_target_adapter(
    mock: ProwlarrMock, *, max_results: int = 100
) -> tuple[ProwlarrClient, httpx.AsyncClient]:
    transport_client = httpx.AsyncClient(
        base_url=mock.base_url,
        transport=mock.transport(),
    )
    return (
        ProwlarrClient(
            base_url=mock.base_url,
            api_key=mock.api_key,
            timeout=1,
            max_results=max_results,
            client=transport_client,
        ),
        transport_client,
    )


async def _close_target_adapter(
    adapter: ProwlarrClient, transport_client: httpx.AsyncClient
) -> None:
    await adapter.aclose()
    await transport_client.aclose()


class _Pansou:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None):
        self.response = response or {"merged_by_type": {"magnet": []}}
        self.error = error

    async def search(self, _query: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.response


class _EventLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def log_event(
        self, event: str, *, fields: dict[str, object] | None = None, **_kwargs: object
    ) -> None:
        self.events.append((event, dict(fields or {})))


def _new_search_service(
    pansou: _Pansou,
    prowlarr: ProwlarrClient,
    *,
    event_logger: _EventLogger | None = None,
) -> SearchService:
    service = SearchService.__new__(SearchService)
    service._pansou = pansou
    service._prowlarr = prowlarr
    service._pansou_limit = asyncio.Semaphore(2)
    service._pansou_timeout = 1
    service._prowlarr_limit = asyncio.Semaphore(2)
    service._prowlarr_state_lock = asyncio.Lock()
    service._prowlarr_usage = {}
    service._prowlarr_idle = {}
    service._share_domains = ("115.com",)
    service._event_logger = event_logger
    return service


def test_prowlarr_public_responses_do_not_expose_api_key():
    assert "api_key" not in ProwlarrSettingsResponse.model_fields
    assert "api_key" not in ProwlarrVerifyResponse.model_fields
    assert set(SearchSourcesResponse.model_fields) == {"pansou", "prowlarr"}


def test_prowlarr_settings_response_has_stable_source_and_revision_fields():
    fields = ProwlarrSettingsResponse.model_fields
    assert {
        "source",
        "enabled",
        "configured",
        "base_url",
        "api_key_configured",
        "api_key_source",
        "last_updated_at",
        "revision",
    } <= set(fields)


async def test_mock_requires_header_auth_and_redacts_credential_from_capture():
    mock = ProwlarrMock([MockResponse.json(load_fixture("search_page_1.json"))])

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
    mock = ProwlarrMock([MockResponse.json(load_fixture("search_page_1.json"))])
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
    assert len(mock.requests) == 1
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
    assert all(
        "next_cursor" not in response for response in (first.json(), second.json())
    )


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
    assert any(
        "\u4e00" <= character <= "\u9fff" for character in payload["suggestion_zh"]
    )


async def test_target_adapter_uses_header_only_and_passes_official_search_query():
    mock = ProwlarrMock([MockResponse.json(load_fixture("search_page_1.json"))])
    adapter, transport_client = _new_target_adapter(mock, max_results=2)
    try:
        await adapter.search(
            "Example Show",
            search_type="tvsearch",
            indexer_ids=[101, 202],
            categories=[5000, 5070],
            limit=2,
            offset=0,
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    request = mock.requests[0]
    assert request.api_key_header_valid is True
    assert request.api_key_in_query is False
    assert request.authorization_header_present is False
    assert request.values("query") == ("Example Show",)
    assert request.values("type") == ("tvsearch",)
    assert request.values("indexerIds") == ("101", "202")
    assert request.values("categories") == ("5000", "5070")
    assert request.values("limit") == ("2",)
    assert request.values("offset") == ("0",)
    assert len(mock.requests) == 1


async def test_target_adapter_distinguishes_torrent_and_excludes_nzb_from_results():
    mock = ProwlarrMock([MockResponse.json(load_fixture("search_page_1.json"))])
    adapter, transport_client = _new_target_adapter(mock, max_results=2)
    try:
        result = await adapter.search("Example Show", search_type="tvsearch", limit=2)
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert result.unsupported_count == 1
    assert len(result.releases) == 1
    torrent = result.releases[0]
    assert torrent.protocol == "torrent"
    assert torrent.info_hash == TORRENT_INFOHASH


async def test_search_service_merges_duplicate_infohash_and_keeps_sources():
    first = ProwlarrMock([MockResponse.json(load_fixture("search_page_1.json"))])
    adapter, transport_client = _new_target_adapter(first, max_results=2)
    pansou = _Pansou(
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": f"magnet:?xt=urn:btih:{TORRENT_INFOHASH}",
                        "note": "Example Show S01E01 1080p",
                        "source": "fixture-pansou",
                    }
                ]
            }
        }
    )
    service = _new_search_service(pansou, adapter)
    private_query = "private-prowlarr-query"
    try:
        successful_pansou, successful_prowlarr, warnings, complete = (
            await service._query_sources(("Example Show",))
        )
        captured_at = datetime.now(UTC)
        normalized = service._merge_normalized_results(
            service._normalize_results(successful_pansou, captured_at),
            service._normalize_prowlarr_results(
                [(private_query, successful_prowlarr[0][1])],
                captured_at,
            ),
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert complete is True
    assert "prowlarr_unsupported_results" in warnings
    assert len(normalized) == 1
    assert normalized[0].canonical_key == f"magnet:{TORRENT_INFOHASH}"
    assert set(normalized[0].metadata["sources"]) == {
        "fixture-pansou",
        "prowlarr",
    }
    assert {
        item["source"] for item in normalized[0].metadata["source_observations"]
    } == {"fixture-pansou", "prowlarr"}

    prowlarr_only = service._normalize_prowlarr_results(
        [
            (private_query, successful_prowlarr[0][1]),
            ("another-private-prowlarr-query", successful_prowlarr[0][1]),
        ],
        captured_at,
    )[0]
    assert "search_queries" not in prowlarr_only.metadata
    assert private_query not in json.dumps(prowlarr_only.metadata)
    assert "search_queries" not in json.dumps(prowlarr_only.metadata)
    assert "api_key" not in json.dumps(prowlarr_only.metadata)
    assert set(prowlarr_only.metadata["source_observations"][0]) == {
        "source",
        "captured_at",
    }

    persisted_metadata = json.dumps(normalized[0].metadata, ensure_ascii=False)
    assert private_query not in persisted_metadata
    assert normalized[0].metadata["search_queries"] == ["Example Show"]
    assert "X-Api-Key" not in persisted_metadata
    summary = _resource_summary(
        Resource(
            id="res_prowlarr_contract",
            kind=normalized[0].kind,
            canonical_key=normalized[0].canonical_key,
            encrypted_url="fixture-cipher",
            name=normalized[0].name,
            source=normalized[0].source,
            captured_at=normalized[0].captured_at,
            expires_at=normalized[0].captured_at,
            metadata_json=persisted_metadata,
        ),
        None,
    )
    public_response = summary.model_dump(mode="json")
    assert "metadata" not in public_response
    assert "search_queries" not in public_response
    assert "api_key" not in json.dumps(public_response, ensure_ascii=False)
    assert private_query not in json.dumps(public_response, ensure_ascii=False)


@pytest.mark.parametrize(
    "failure",
    [
        MockResponse.upstream_timeout(),
        MockResponse.failure(429, {"error": "fixture_rate_limited"}),
        MockResponse.failure(500, {"error": "fixture_failure"}),
    ],
)
async def test_search_service_keeps_healthy_source_on_prowlarr_failure(
    failure: MockResponse,
):
    mock = ProwlarrMock([failure])
    adapter, transport_client = _new_target_adapter(mock)
    event_logger = _EventLogger()
    service = _new_search_service(
        _Pansou(
            {
                "merged_by_type": {
                    "magnet": [
                        {
                            "url": f"magnet:?xt=urn:btih:{TORRENT_INFOHASH}",
                            "note": "Example Show S01E01 1080p",
                            "source": "fixture-pansou",
                        }
                    ]
                }
            }
        ),
        adapter,
        event_logger=event_logger,
    )
    try:
        successful_pansou, successful_prowlarr, warnings, complete = (
            await service._query_sources(("Example Show",))
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert len(successful_pansou) == 1
    assert successful_prowlarr == []
    assert "prowlarr_query_failed:1" in warnings
    assert complete is False
    assert event_logger.events == [
        (
            "search.source_degraded",
            {
                "source": "Prowlarr",
                "status": "degraded",
                "count": 1,
                "total": 1,
            },
        )
    ]


async def test_target_adapter_stops_pagination_after_short_page():
    mock = ProwlarrMock(
        [
            MockResponse.json(load_fixture("search_page_1.json")),
            MockResponse.json(load_fixture("search_page_2.json")),
        ]
    )
    adapter, transport_client = _new_target_adapter(mock, max_results=5)
    try:
        result = await adapter.search(
            "Example Show", search_type="tvsearch", page_size=2
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert [request.values("offset") for request in mock.requests] == [
        ("0",),
        ("2",),
    ]
    assert len(result.releases) == 2
    assert result.unsupported_count == 1


@pytest.mark.parametrize(
    ("failure", "exception_type", "error_code"),
    [
        (
            MockResponse.failure(408, {"error": "fixture_timeout"}),
            ProwlarrTimeoutError,
            "prowlarr_timeout",
        ),
        (
            MockResponse.failure(429, {"error": "fixture_rate_limited"}),
            ProwlarrRateLimitError,
            "prowlarr_rate_limited",
        ),
        (
            MockResponse.failure(503, {"error": "fixture_server_error"}),
            ProwlarrServerError,
            "prowlarr_server_error",
        ),
    ],
)
async def test_target_adapter_classifies_read_only_upstream_failures(
    failure: MockResponse, exception_type: type[Exception], error_code: str
):
    mock = ProwlarrMock([failure])
    adapter, transport_client = _new_target_adapter(mock)
    try:
        with pytest.raises(exception_type) as error:
            await adapter.search("Example Show")
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert error.value.error_code == error_code
    assert mock.requests[0].api_key_in_query is False
    assert "fixture_" not in str(error.value)


async def test_target_adapter_honors_bounded_retry_after_without_exposing_header():
    mock = ProwlarrMock(
        [
            MockResponse.failure(
                429,
                {"error": "fixture_rate_limited"},
                headers={"Retry-After": "42"},
            )
        ]
    )
    adapter, transport_client = _new_target_adapter(mock)
    try:
        with pytest.raises(ProwlarrRateLimitError) as error:
            await adapter.search("Example Show")
        health = adapter.health_tracker.snapshot()
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert error.value.retry_after_seconds == 42
    assert 0 < health.retry_after_seconds <= error.value.retry_after_seconds
    assert "Retry-After" not in str(error.value)


async def test_target_adapter_opens_local_circuit_after_repeated_server_failures():
    mock = ProwlarrMock(
        [
            MockResponse.failure(500, {"error": "fixture_server_error"})
            for _ in range(3)
        ]
    )
    adapter, transport_client = _new_target_adapter(mock)
    try:
        for _ in range(3):
            with pytest.raises(ProwlarrServerError):
                await adapter.search("Example Show")
        with pytest.raises(ProwlarrCircuitOpenError):
            await adapter.search("Example Show")
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert len(mock.requests) == 3


async def test_target_adapter_marks_continuous_full_page_as_truncated():
    page = load_fixture("search_page_1.json")[:1]
    mock = ProwlarrMock([MockResponse.json(page) for _ in range(20)])
    adapter, transport_client = _new_target_adapter(mock, max_results=50)
    try:
        result = await adapter.search(
            "Example Show", search_type="tvsearch", page_size=1
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    assert len(mock.requests) == 20
    assert result.truncated is True


async def test_target_maps_prowlarr_upstream_failure_to_chinese_error_code():
    mock = ProwlarrMock([MockResponse.failure(429, {"error": "fixture_rate_limited"})])
    adapter, transport_client = _new_target_adapter(mock)
    service = _new_search_service(
        _Pansou(error=RuntimeError("fixture PanSou failure")), adapter
    )
    try:
        successful_pansou, successful_prowlarr, warnings, complete = (
            await service._query_sources(("Example Show",))
        )
    finally:
        await _close_target_adapter(adapter, transport_client)

    payload = build_error_payload(
        "resource_search_unavailable",
        503,
        request_id="request-contract",
        correlation_id="correlation-contract",
    )
    assert successful_pansou == []
    assert successful_prowlarr == []
    assert "prowlarr_query_failed:1" in warnings
    assert complete is False
    assert payload["code"] == "resource_search_unavailable"
    assert any("\u4e00" <= character <= "\u9fff" for character in payload["message_zh"])


async def test_search_failure_log_uses_safe_chinese_error_code():
    event_logger = _EventLogger()
    service = SearchService.__new__(SearchService)
    service._event_logger = event_logger

    async def fail(*_args: object, **_kwargs: object):
        raise SearchUnavailable("resource_search_unavailable")

    service._search_impl = fail
    with pytest.raises(SearchUnavailable):
        await service.search(123)

    assert event_logger.events[0][0] == "search.started"
    assert event_logger.events[1] == (
        "search.failed",
        {
            "media_type": "movie",
            "season": "all",
            "status": "failed",
            "error_code": "resource_search_unavailable",
            "duration_ms": event_logger.events[1][1]["duration_ms"],
        },
    )
