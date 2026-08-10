"""Normalize and deduplicate PanSou resources."""

import base64
import binascii
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from watch_assistant.adapters.prowlarr import ProwlarrRelease
from watch_assistant.schemas import NormalizedResource, ResourceKind

HEX_INFOHASH = re.compile(r"[0-9A-Fa-f]{40}")
BASE32_INFOHASH = re.compile(r"[A-Z2-7a-z2-7]{32}")
SHARE_PATH = re.compile(r"/(?:s|share)/([A-Za-z0-9_-]+)(?:/|$)")
SIZE_VALUE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|TiB|B|KB|MB|GB|TB)\s*$",
    re.IGNORECASE,
)
SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024**2,
    "MIB": 1024**2,
    "GB": 1024**3,
    "GIB": 1024**3,
    "TB": 1024**4,
    "TIB": 1024**4,
}
LABEL_PRIORITY = {"dn": 1, "note": 2, "name": 3}
SAFE_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


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
    ordered_keys: list[str] = []
    resources_by_key: dict[str, NormalizedResource] = {}

    for category, items in merged.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            resource = _normalize_item(item, str(category), domains, fallback_time)
            if resource is None:
                continue
            existing = resources_by_key.get(resource.canonical_key)
            if existing is None:
                ordered_keys.append(resource.canonical_key)
                resources_by_key[resource.canonical_key] = resource
            else:
                resources_by_key[resource.canonical_key] = merge_normalized_resources(
                    existing, resource
                )
    return [resources_by_key[key] for key in ordered_keys]


def normalize_prowlarr(
    releases: Iterable[ProwlarrRelease],
    *,
    captured_at: datetime | None = None,
) -> list[NormalizedResource]:
    """Map verified torrent releases into the common magnet resource shape."""
    fallback_time = captured_at or datetime.now(UTC)
    resources: list[NormalizedResource] = []
    for release in releases:
        magnet = _parse_magnet(release.magnet_url)
        if magnet is None:
            continue
        canonical_key, display_name = magnet
        url = release.magnet_url
        if display_name is None:
            url = _add_magnet_dn(url, release.title)
        metadata: dict[str, Any] = {
            "category": "magnet",
            "prowlarr_protocol": release.protocol,
        }
        if release.indexer_id is not None:
            metadata["prowlarr_indexer_id"] = release.indexer_id
        if release.size_bytes is not None:
            metadata["size_source"] = "prowlarr"
        if release.seeders is not None:
            metadata["seeders_source"] = "prowlarr"
        resources.append(
            NormalizedResource(
                kind=ResourceKind.MAGNET,
                canonical_key=canonical_key,
                name=release.title,
                url=url,
                size_bytes=release.size_bytes,
                seeders=release.seeders,
                source="prowlarr",
                captured_at=_parse_datetime(release.publish_date) or fallback_time,
                metadata=metadata,
            )
        )
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
    magnet = _parse_magnet(url)
    share_key = None if magnet else _share_key(url, share_domains)
    if magnet:
        kind = ResourceKind.MAGNET
        canonical_key, magnet_dn = magnet
    elif share_key:
        kind = ResourceKind.SHARE
        canonical_key = share_key
        magnet_dn = None
    else:
        return None

    note = item.get("note") if isinstance(item.get("note"), str) else ""
    if kind == ResourceKind.MAGNET:
        label = _select_magnet_label(item, note, magnet_dn)
        if label is None:
            return None
        name, label_source = label
        if magnet_dn is None:
            url = _add_magnet_dn(url, name)
    else:
        name = note.strip() or "PanSou resource"
        label_source = None
    source = normalize_source_id(item.get("source"))
    raw_datetime = item.get("datetime")
    captured_at = _parse_datetime(raw_datetime) or fallback_time
    password = item.get("password")
    if not isinstance(password, str) or not password:
        password = None
    metadata: dict[str, Any] = {
        "category": category,
        "sources": [source],
        "source_observations": [_source_observation(source, captured_at)],
    }
    if label_source:
        metadata["label_source"] = label_source
    if note:
        metadata["note"] = note
    if isinstance(raw_datetime, str):
        metadata["datetime"] = raw_datetime
    if isinstance(item.get("images"), list):
        metadata["images"] = item["images"]
    size_bytes = _parse_size(item.get("size"))
    seeders = _parse_nonnegative_int(item.get("seeders"))
    if size_bytes is not None and item.get("size_source") in {
        "pansou",
        "inspection",
    }:
        metadata["size_source"] = item["size_source"]
    if seeders is not None and item.get("seeders_source") == "pansou":
        metadata["seeders_source"] = "pansou"
        observed_at = item.get("seeders_observed_at")
        if isinstance(observed_at, str) and observed_at.strip():
            metadata["seeders_observed_at"] = observed_at.strip()

    return NormalizedResource(
        kind=kind,
        canonical_key=canonical_key,
        name=name,
        url=url,
        password=password,
        size_bytes=size_bytes,
        seeders=seeders,
        source=source,
        captured_at=captured_at,
        metadata=metadata,
    )


def _parse_magnet(url: str) -> tuple[str, str | None] | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "magnet"
        or parsed.netloc
        or parsed.path
        or parsed.fragment
        or not parsed.query
    ):
        return None
    canonical_key = None
    display_name = None
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        normalized_key = key.casefold()
        if normalized_key == "dn" and display_name is None and value.strip():
            display_name = value.strip()
        if normalized_key != "xt" or not value.casefold().startswith("urn:btih:"):
            continue
        infohash = value[9:]
        if HEX_INFOHASH.fullmatch(infohash):
            decoded = infohash.casefold()
        elif BASE32_INFOHASH.fullmatch(infohash):
            try:
                decoded = base64.b32decode(infohash.upper()).hex()
            except (binascii.Error, ValueError):
                continue
        else:
            continue
        if canonical_key is None and decoded != "0" * 40:
            canonical_key = f"magnet:{decoded}"
    if canonical_key is None:
        return None
    return canonical_key, display_name


def _select_magnet_label(
    item: dict[str, Any],
    note: str,
    magnet_dn: str | None,
) -> tuple[str, str] | None:
    candidates = (
        ("name", item.get("name")),
        ("note", note),
        ("dn", magnet_dn),
    )
    for source, candidate in candidates:
        if not isinstance(candidate, str):
            continue
        label = candidate.strip()
        if _is_displayable_label(label):
            return label, source
    return None


def _is_displayable_label(label: str) -> bool:
    if not label or not any(character.isalnum() for character in label):
        return False
    folded = label.casefold()
    if folded.startswith(("magnet:", "urn:btih:")):
        return False
    return not HEX_INFOHASH.fullmatch(label) and not BASE32_INFOHASH.fullmatch(label)


def _add_magnet_dn(url: str, name: str) -> str:
    parsed = urlsplit(url)
    query = f"{parsed.query}&{urlencode({'dn': name})}"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def merge_normalized_resources(
    existing: NormalizedResource,
    candidate: NormalizedResource,
) -> NormalizedResource:
    """Choose the richer duplicate and fill its missing optional fields."""
    if _resource_richness(candidate) > _resource_richness(existing):
        base, other = candidate, existing
    else:
        base, other = existing, candidate
    metadata = {**other.metadata, **base.metadata}
    metadata["sources"] = _merge_source_ids(
        existing.metadata.get("sources"),
        existing.source,
        candidate.metadata.get("sources"),
        candidate.source,
    )
    metadata["source_observations"] = _merge_source_observations(
        existing.metadata.get("source_observations"),
        [_source_observation(existing.source, existing.captured_at)],
        candidate.metadata.get("source_observations"),
        [_source_observation(candidate.source, candidate.captured_at)],
    )
    size_bytes, size_owner = _merge_optional_field(
        base, other, "size_bytes", "size_source"
    )
    seeders, seeders_owner = _merge_optional_field(
        base, other, "seeders", "seeders_source"
    )
    _set_field_source(metadata, "size_source", size_owner)
    _set_field_source(metadata, "seeders_source", seeders_owner)
    if metadata.get("seeders_source") == "pansou":
        observed_at = seeders_owner.metadata.get("seeders_observed_at")
        if isinstance(observed_at, str) and observed_at.strip():
            metadata["seeders_observed_at"] = observed_at
        else:
            metadata.pop("seeders_observed_at", None)
    else:
        metadata.pop("seeders_observed_at", None)
    return base.model_copy(
        update={
            "password": base.password or other.password,
            "size_bytes": size_bytes,
            "seeders": seeders,
            "metadata": metadata,
        }
    )


def _merge_optional_field(
    base: NormalizedResource,
    other: NormalizedResource,
    field: str,
    source_key: str,
) -> tuple[int | None, NormalizedResource | None]:
    base_value = getattr(base, field)
    other_value = getattr(other, field)
    if base_value is None:
        return other_value, other if other_value is not None else None
    if other_value is None:
        return base_value, base
    base_source = base.metadata.get(source_key)
    other_source = other.metadata.get(source_key)
    # 结构化字段(非插件标记)胜出:插件带来的 pansou 标记值可能过期,
    # 同 infohash 去重时不能覆盖条目的原生结构化取值;
    # 来源标记只随胜出的条目保留(test_duplicate_infohash_* 锁定此语义)。
    if base_source == "pansou" and other_source != "pansou":
        return other_value, other
    if other_source == "pansou" and base_source != "pansou":
        return base_value, base
    return base_value, base


def _set_field_source(
    metadata: dict[str, Any],
    source_key: str,
    owner: NormalizedResource | None,
) -> None:
    source = owner.metadata.get(source_key) if owner is not None else None
    allowed = {"pansou", "inspection"} if source_key == "size_source" else {"pansou"}
    if source in allowed:
        metadata[source_key] = source
    else:
        metadata.pop(source_key, None)


def _resource_richness(resource: NormalizedResource) -> tuple[int, int, int, int, int]:
    optional_fields = sum(
        value is not None
        for value in (resource.password, resource.size_bytes, resource.seeders)
    )
    label_priority = LABEL_PRIORITY.get(resource.metadata.get("label_source"), 0)
    return (
        label_priority,
        optional_fields,
        len(resource.metadata),
        len(resource.name),
        len(resource.url),
    )


def _share_key(url: str, allowed_domains: tuple[str, ...]) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
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


def normalize_source_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "PanSou"
    source = value.strip()
    if SAFE_SOURCE.fullmatch(source):
        return source
    return "source:" + sha256(source.encode("utf-8")).hexdigest()[:16]


def _source_observation(source: str, captured_at: datetime) -> dict[str, str]:
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    else:
        captured_at = captured_at.astimezone(UTC)
    return {
        "source": normalize_source_id(source),
        "captured_at": captured_at.isoformat(),
    }


def _merge_source_ids(*values: object) -> list[str]:
    sources: list[str] = []
    for value in values:
        candidates = (
            [value]
            if isinstance(value, str)
            else value
            if isinstance(value, list)
            else []
        )
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = normalize_source_id(candidate)
            if normalized not in sources:
                sources.append(normalized)
    return sources


def _merge_source_observations(*values: object) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            captured_at = item.get("captured_at")
            if not isinstance(source, str) or not isinstance(captured_at, str):
                continue
            normalized_source = normalize_source_id(source)
            normalized_time = captured_at.strip()[:64]
            if not normalized_time:
                continue
            observation = (normalized_source, normalized_time)
            if observation in seen:
                continue
            seen.add(observation)
            merged.append(
                {"source": normalized_source, "captured_at": normalized_time}
            )
    return merged


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
    try:
        return int(float(match.group(1)) * SIZE_MULTIPLIERS[match.group(2).upper()])
    except (KeyError, OverflowError, ValueError):
        return None


def _parse_nonnegative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
