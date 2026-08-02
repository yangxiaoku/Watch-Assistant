"""Read-only Prowlarr search adapter.

Only the public search endpoint is used here.  Indexer administration and
download actions intentionally remain outside this adapter.
"""

import base64
import binascii
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from watch_assistant.services.source_health import SourceHealthTracker


class ProwlarrError(RuntimeError):
    """Base error for safe, user-facing Prowlarr degradation."""

    error_code = "prowlarr_unavailable"
    failure_kind = "unavailable"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProwlarrAuthError(ProwlarrError):
    """Prowlarr rejected the configured API key."""

    error_code = "prowlarr_auth_required"
    failure_kind = "auth"


class ProwlarrInvalidResponseError(ProwlarrError):
    """Prowlarr returned a response outside the verified contract."""

    error_code = "prowlarr_invalid_response"
    failure_kind = "invalid_response"


class ProwlarrTimeoutError(ProwlarrError):
    """The verified read-only request exceeded its timeout."""

    error_code = "prowlarr_timeout"
    failure_kind = "timeout"


class ProwlarrRateLimitError(ProwlarrError):
    """Prowlarr asked the caller to slow down."""

    error_code = "prowlarr_rate_limited"
    failure_kind = "rate_limited"

    def __init__(
        self,
        message: str = "Prowlarr rate limit reached",
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_seconds = retry_after_seconds


class ProwlarrServerError(ProwlarrError):
    """Prowlarr returned a server-side 5xx response."""

    error_code = "prowlarr_server_error"
    failure_kind = "server_error"


class ProwlarrCircuitOpenError(ProwlarrError):
    """The local source circuit is open and no upstream request was made."""

    error_code = "prowlarr_circuit_open"
    failure_kind = "circuit_open"


@dataclass(frozen=True, slots=True)
class ProwlarrRelease:
    title: str
    magnet_url: str
    info_hash: str
    size_bytes: int | None
    seeders: int | None
    indexer: str | None
    indexer_id: int | None
    guid: str | None
    publish_date: str | None
    protocol: str


@dataclass(frozen=True, slots=True)
class ProwlarrSearchResult:
    releases: tuple[ProwlarrRelease, ...]
    unsupported_count: int = 0
    truncated: bool = False


_HEX_INFOHASH = re.compile(r"[0-9a-fA-F]{40}")
_BASE32_INFOHASH = re.compile(r"[A-Z2-7a-z2-7]{32}")
_MAX_RESULTS = 500
_MAX_PAGES = 20
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 100
_MAX_TEXT_LENGTH = 500


class ProwlarrClient:
    """Call Prowlarr's public search API without exposing its API key."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 12.0,
        max_results: int = _MAX_RESULTS,
        client: httpx.AsyncClient | None = None,
        health_tracker: SourceHealthTracker | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_results = max(1, min(max_results, _MAX_RESULTS))
        self._health = health_tracker or SourceHealthTracker()
        self._owns_client = client is None
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=base_url.rstrip("/") + "/",
                headers={"X-Api-Key": api_key},
            )
        else:
            self._client = client
            self._client.headers.pop("Authorization", None)
            self._client.headers["X-Api-Key"] = api_key

    async def search(
        self,
        keyword: str,
        *,
        search_type: str = "search",
        indexer_ids: Iterable[int] | None = None,
        categories: Iterable[int] | None = None,
        limit: int | None = None,
        offset: int = 0,
        page_size: int | None = None,
    ) -> ProwlarrSearchResult:
        """Search the official bare-array endpoint with bounded pagination."""
        if not self._health.allow_request():
            raise ProwlarrCircuitOpenError("Prowlarr circuit is open")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        result_limit = self._max_results if limit is None else limit
        if type(result_limit) is not int or result_limit < 1:
            raise ValueError("limit must be a positive integer")
        result_limit = min(result_limit, self._max_results)
        requested_page_size = (
            page_size
            if page_size is not None
            else result_limit
            if limit is not None
            else min(result_limit, _DEFAULT_PAGE_SIZE)
        )
        if type(requested_page_size) is not int or requested_page_size < 1:
            raise ValueError("page_size must be a positive integer")
        requested_page_size = min(requested_page_size, _MAX_PAGE_SIZE)
        indexer_params = _array_params("indexerIds", indexer_ids)
        category_params = _array_params("categories", categories)

        releases: list[ProwlarrRelease] = []
        unsupported_count = 0
        items_seen = 0
        current_offset = offset
        truncated = False
        try:
            for _page_number in range(_MAX_PAGES):
                remaining = result_limit - items_seen
                if remaining <= 0:
                    break
                request_limit = min(requested_page_size, remaining)
                params: list[tuple[str, str]] = [
                    ("query", keyword),
                    ("type", search_type),
                ]
                params.extend(indexer_params)
                params.extend(category_params)
                params.extend(
                    (("limit", str(request_limit)), ("offset", str(current_offset)))
                )
                payload = await self._request_page(params)
                if len(payload) > request_limit:
                    raise ProwlarrInvalidResponseError(
                        "Unexpected Prowlarr response shape"
                    )

                items_seen += len(payload)
                for item in payload:
                    if not isinstance(item, dict):
                        raise ProwlarrInvalidResponseError(
                            "Unexpected Prowlarr response shape"
                        )
                    release = _parse_release(item)
                    if release is None:
                        unsupported_count += 1
                    else:
                        releases.append(release)

                if len(payload) < request_limit:
                    break
                next_offset = current_offset + len(payload)
                if next_offset <= current_offset:
                    raise ProwlarrInvalidResponseError(
                        "Unexpected Prowlarr response shape"
                    )
                current_offset = next_offset
            else:
                # A full final page means the bounded paginator stopped before
                # the upstream advertised an end. Preserve that fact for aggregation.
                truncated = items_seen < result_limit
        except ProwlarrError as exc:
            self._health.record_failure(
                exc.error_code,
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
            )
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            error = ProwlarrTimeoutError("Prowlarr request timed out")
            self._health.record_failure(error.error_code)
            raise error from exc
        self._health.record_success()
        return ProwlarrSearchResult(tuple(releases), unsupported_count, truncated)

    @property
    def health_tracker(self) -> SourceHealthTracker:
        return self._health

    async def _request_page(self, params: list[tuple[str, str]]) -> list[Any]:
        try:
            response = await self._client.get(
                "api/v1/search",
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise ProwlarrAuthError(
                    "Prowlarr authentication failed",
                    status_code=exc.response.status_code,
                ) from exc
            if exc.response.status_code == 408:
                raise ProwlarrTimeoutError(
                    "Prowlarr request timed out", status_code=408
                ) from exc
            if exc.response.status_code == 429:
                raise ProwlarrRateLimitError(
                    retry_after_seconds=_retry_after_seconds(exc.response)
                ) from exc
            if 500 <= exc.response.status_code <= 599:
                raise ProwlarrServerError(
                    "Prowlarr server error", status_code=exc.response.status_code
                ) from exc
            raise ProwlarrError(
                "Prowlarr request failed", status_code=exc.response.status_code
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProwlarrTimeoutError("Prowlarr request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProwlarrError("Prowlarr request failed") from exc

        try:
            payload = response.json()
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProwlarrInvalidResponseError(
                "Unexpected Prowlarr response shape"
            ) from exc
        if not isinstance(payload, list):
            raise ProwlarrInvalidResponseError("Unexpected Prowlarr response shape")
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _array_params(name: str, values: Iterable[int] | None) -> list[tuple[str, str]]:
    if values is None:
        return []
    params: list[tuple[str, str]] = []
    for value in values:
        if type(value) is not int:
            raise ValueError(f"{name} must contain integers")
        params.append((name, str(value)))
    return params


def _parse_release(item: dict[str, Any]) -> ProwlarrRelease | None:
    protocol = _text(_field(item, "protocol", "Protocol"))
    if protocol is None or protocol.casefold() != "torrent":
        return None
    title = _text(_field(item, "title", "Title"))
    if title is None:
        return None

    magnet_url = _text(
        _field(
            item,
            "magnetUrl",
            "MagnetUrl",
            "magnet_url",
            "downloadUrl",
            "DownloadUrl",
        )
    )
    info_hash = _normalise_infohash(_field(item, "infoHash", "InfoHash"))
    if magnet_url is not None:
        magnet_info_hash = _infohash_from_magnet(magnet_url)
        if magnet_info_hash is not None:
            info_hash = magnet_info_hash
        else:
            magnet_url = None
    if magnet_url is None and info_hash is not None:
        magnet_url = "magnet:?" + urlencode(
            {"xt": f"urn:btih:{info_hash}", "dn": title}
        )
    if magnet_url is None or info_hash is None:
        return None

    return ProwlarrRelease(
        title=title,
        magnet_url=magnet_url,
        info_hash=info_hash,
        size_bytes=_nonnegative_int(_field(item, "size", "Size")),
        seeders=_nonnegative_int(_field(item, "seeders", "Seeders")),
        indexer=_text(_field(item, "indexer", "Indexer")),
        indexer_id=_nonnegative_int(_field(item, "indexerId", "IndexerId")),
        guid=_text(_field(item, "guid", "Guid")),
        publish_date=_text(_field(item, "publishDate", "PublishDate")),
        protocol="torrent",
    )


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_MAX_TEXT_LENGTH] if value else None


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _normalise_infohash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _HEX_INFOHASH.fullmatch(value):
        return value.casefold()
    if not _BASE32_INFOHASH.fullmatch(value):
        return None
    try:
        return base64.b32decode(value.upper()).hex()
    except (binascii.Error, ValueError):
        return None


def _infohash_from_magnet(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() != "magnet":
        return None
    for key, raw in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() == "xt" and raw.casefold().startswith("urn:btih:"):
            return _normalise_infohash(raw[9:])
    return None


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    if not value.isdigit():
        return None
    seconds = int(value)
    return seconds if 0 < seconds <= 3600 else None
