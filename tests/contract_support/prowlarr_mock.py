"""Offline Prowlarr HTTP fixture based on the public search API contract.

The fixture deliberately records only credential-safe request metadata. It
never stores or renders the generated API key in a request record.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

PROWLARR_SEARCH_PATH = "/api/v1/search"
PROWLARR_BASE_URL = "https://prowlarr.fixture.invalid"
_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "prowlarr"


@dataclass(frozen=True, slots=True)
class MockResponse:
    """A deterministic response or transport failure for one mock call."""

    status_code: int = 200
    payload: Any = None
    timeout: bool = False

    @classmethod
    def json(cls, payload: Any, *, status_code: int = 200) -> MockResponse:
        return cls(status_code=status_code, payload=payload)

    @classmethod
    def failure(cls, status_code: int, payload: Any) -> MockResponse:
        return cls(status_code=status_code, payload=payload)

    @classmethod
    def upstream_timeout(cls) -> MockResponse:
        return cls(timeout=True)


@dataclass(frozen=True, slots=True)
class RecordedSearchRequest:
    """Credential-safe metadata captured from one request."""

    method: str
    path: str
    query_items: tuple[tuple[str, str], ...]
    header_names: tuple[str, ...]
    api_key_header_present: bool
    api_key_header_valid: bool
    api_key_in_query: bool
    authorization_header_present: bool

    def values(self, name: str) -> tuple[str, ...]:
        folded = name.casefold()
        return tuple(value for key, value in self.query_items if key.casefold() == folded)


def load_fixture(name: str) -> Any:
    """Load a checked-in synthetic response without accepting path traversal."""

    path = Path(name)
    if path.name != name or path.suffix.casefold() != ".json":
        raise ValueError("fixture name must be a JSON file name")
    return json.loads((_FIXTURE_ROOT / path).read_text(encoding="utf-8"))


class ProwlarrMock:
    """An in-memory Prowlarr search endpoint with no network listener."""

    def __init__(self, responses: Iterable[MockResponse]) -> None:
        self._responses = deque(responses)
        self._api_key = uuid4().hex
        self._requests: list[RecordedSearchRequest] = []

    @property
    def base_url(self) -> str:
        return PROWLARR_BASE_URL

    @property
    def api_key(self) -> str:
        """Return the per-test credential; it is never stored in captures."""

        return self._api_key

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    @property
    def requests(self) -> tuple[RecordedSearchRequest, ...]:
        return tuple(self._requests)

    def client(self, *, timeout: float = 1.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport(),
            timeout=timeout,
        )

    def transport(self) -> httpx.MockTransport:
        """Return a transport that cannot open a real network connection."""

        return httpx.MockTransport(self._handle)

    def __repr__(self) -> str:
        return f"ProwlarrMock(requests={len(self._requests)})"

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        query_items = tuple(
            (
                name,
                "<redacted>" if name.casefold() == "apikey" else value,
            )
            for name, value in request.url.params.multi_items()
        )
        header_names = tuple(sorted(name.casefold() for name in request.headers))
        header_value = request.headers.get("X-Api-Key")
        query_has_key = any(
            name.casefold() == "apikey" for name, _value in query_items
        )
        self._requests.append(
            RecordedSearchRequest(
                method=request.method,
                path=request.url.path,
                query_items=query_items,
                header_names=header_names,
                api_key_header_present=header_value is not None,
                api_key_header_valid=header_value == self._api_key,
                api_key_in_query=query_has_key,
                authorization_header_present="authorization" in header_names,
            )
        )

        if request.method != "GET" or request.url.path != PROWLARR_SEARCH_PATH:
            return httpx.Response(404, json={"error": "fixture_route_not_found"}, request=request)
        if header_value != self._api_key:
            return httpx.Response(401, json={"error": "fixture_auth_required"}, request=request)
        if not self._responses:
            return httpx.Response(500, json={"error": "fixture_response_exhausted"}, request=request)

        response = self._responses.popleft()
        if response.timeout:
            raise httpx.ReadTimeout("synthetic upstream timeout", request=request)
        return httpx.Response(
            response.status_code,
            json=response.payload,
            headers={"content-type": "application/json"},
            request=request,
        )
