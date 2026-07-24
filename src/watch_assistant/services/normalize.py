"""Normalize and deduplicate PanSou resources."""

import base64
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from watch_assistant.schemas import NormalizedResource, ResourceKind

HEX_INFOHASH = re.compile(r"[0-9A-Fa-f]{40}")
BASE32_INFOHASH = re.compile(r"[A-Z2-7a-z2-7]{32}")
SHARE_PATH = re.compile(r"/(?:s|share)/([A-Za-z0-9_-]+)(?:/|$)")
SIZE_VALUE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\s*$", re.IGNORECASE
)
SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


def normalize_pansou(
    data: dict[str, Any],
    *,
    share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    captured_at: datetime | None = None,
) -> list[NormalizedResource]:
    merged = data.get("merged_by_type")
    if not isinstance(merged, dict):
        return []
    fallback_time = captured_at or datetime.now(UTC)
    domains = tuple(_normalize_domain(domain) for domain in share_domains)
    resources: list[NormalizedResource] = []
    seen: set[str] = set()

    for category, items in merged.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            resource = _normalize_item(item, str(category), domains, fallback_time)
            if resource is None or resource.canonical_key in seen:
                continue
            seen.add(resource.canonical_key)
            resources.append(resource)
    return resources


def _normalize_item(
    item: dict[str, Any],
    category: str,
    share_domains: tuple[str, ...],
    fallback_time: datetime,
) -> NormalizedResource | None:
    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    magnet_key = _magnet_key(url)
    share_key = None if magnet_key else _share_key(url, share_domains)
    if magnet_key:
        kind = ResourceKind.MAGNET
        canonical_key = magnet_key
    elif share_key:
        kind = ResourceKind.SHARE
        canonical_key = share_key
    else:
        return None

    note = item.get("note") if isinstance(item.get("note"), str) else ""
    source = item.get("source") if isinstance(item.get("source"), str) else "PanSou"
    raw_datetime = item.get("datetime")
    password = item.get("password")
    if not isinstance(password, str) or not password:
        password = None
    metadata: dict[str, Any] = {"category": category}
    if note:
        metadata["note"] = note
    if isinstance(raw_datetime, str):
        metadata["datetime"] = raw_datetime
    if isinstance(item.get("images"), list):
        metadata["images"] = item["images"]

    return NormalizedResource(
        kind=kind,
        canonical_key=canonical_key,
        name=note.strip() or "PanSou resource",
        url=url,
        password=password,
        size_bytes=_parse_size(item.get("size")),
        seeders=_parse_nonnegative_int(item.get("seeders")),
        source=source,
        captured_at=_parse_datetime(raw_datetime) or fallback_time,
        metadata=metadata,
    )


def _magnet_key(url: str) -> str | None:
    if urlsplit(url).scheme.casefold() != "magnet":
        return None
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
        if key.casefold() != "xt" or not value.casefold().startswith("urn:btih:"):
            continue
        infohash = value[9:]
        if HEX_INFOHASH.fullmatch(infohash):
            return f"magnet:{infohash.casefold()}"
        if BASE32_INFOHASH.fullmatch(infohash):
            try:
                decoded = base64.b32decode(infohash.upper()).hex()
            except ValueError:
                return None
            return f"magnet:{decoded}"
    return None


def _share_key(url: str, allowed_domains: tuple[str, ...]) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    normalized_host = host.removeprefix("www.")
    if normalized_host not in allowed_domains:
        return None
    match = SHARE_PATH.search(parsed.path)
    if not match:
        return None
    return f"share:{normalized_host}:{match.group(1)}"


def _normalize_domain(domain: str) -> str:
    normalized = domain.casefold().strip().rstrip(".")
    return normalized.removeprefix("www.")


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_size(value: object) -> int | None:
    if type(value) is int:
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = SIZE_VALUE.fullmatch(value)
    if not match:
        return None
    return int(float(match.group(1)) * SIZE_MULTIPLIERS[match.group(2).upper()])


def _parse_nonnegative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
