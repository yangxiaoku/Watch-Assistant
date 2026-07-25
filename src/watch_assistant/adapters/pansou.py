"""PanSou search and share-link validation adapter."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx


class PanSouError(RuntimeError):
    pass


class LinkCheckState(StrEnum):
    OK = "ok"
    BAD = "bad"
    LOCKED = "locked"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class LinkCheckItem:
    url: str
    password: str | None


_PLUGIN_SIZE = re.compile(
    r"(?<![\w-])(?P<label>大小|文件大小)\s*[:：]\s*"
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\b",
    re.IGNORECASE,
)
_PLUGIN_SEEDERS = re.compile(
    r"(?<![\w-])(?P<label>做种|Seeders)\s*[:：]\s*(?P<number>\d+)(?!\w)",
    re.IGNORECASE,
)
_SIZE_MULTIPLIERS = {
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


class PanSouClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))

    async def search(self, keyword: str) -> dict[str, Any]:
        try:
            response = await self._client.get(
                "/api/search",
                params={"kw": keyword, "res": "all"},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PanSouError("PanSou request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PanSouError("Unexpected PanSou response shape") from exc

        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise PanSouError("Unexpected PanSou response shape")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PanSouError("Unexpected PanSou response shape")
        merged_by_type = data.get("merged_by_type")
        if merged_by_type is None and data.get("total") == 0:
            merged_by_type = {}
        if not isinstance(merged_by_type, dict):
            raise PanSouError("Unexpected PanSou response shape")
        if not all(isinstance(items, list) for items in merged_by_type.values()):
            raise PanSouError("Unexpected PanSou response shape")
        return {
            **data,
            "merged_by_type": _enrich_magnet_metadata(data, merged_by_type),
        }

    async def check_links(
        self,
        items: list[LinkCheckItem],
        *,
        batch_size: int = 10,
    ) -> list[LinkCheckState]:
        states: list[LinkCheckState] = []
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            payload = {
                "items": [
                    {
                        "disk_type": "115",
                        "url": item.url,
                        **(
                            {"password": item.password}
                            if item.password is not None
                            else {}
                        ),
                    }
                    for item in batch
                ]
            }
            try:
                response = await self._client.post(
                    "/api/check/links",
                    json=payload,
                    timeout=max(self._timeout, len(batch) * 12.0),
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise PanSouError("PanSou link check failed") from exc
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise PanSouError("Unexpected PanSou link check response shape")
            if not all(isinstance(result, dict) for result in results):
                raise PanSouError("Unexpected PanSou link check response shape")
            try:
                states.extend(LinkCheckState(result["state"]) for result in results)
            except (KeyError, ValueError) as exc:
                raise PanSouError(
                    "Unexpected PanSou link check response shape"
                ) from exc
            if len(states) != offset + len(batch):
                raise PanSouError("Unexpected PanSou link check response shape")
        return states

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _enrich_magnet_metadata(
    data: dict[str, Any],
    merged_by_type: dict[str, list[Any]],
) -> dict[str, list[Any]]:
    magnets = merged_by_type.get("magnet")
    results = data.get("results")
    if not isinstance(magnets, list) or not isinstance(results, list):
        return merged_by_type

    observed_at = datetime.now(UTC).isoformat()
    plugin_by_hash: dict[str, dict[str, int]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        plugin = result.get("source") or result.get("plugin")
        if plugin not in {"plugin:nyaa", "plugin:thepiratebay"}:
            continue
        links = result.get("links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            infohash = _magnet_infohash(link.get("url"))
            if infohash is None:
                continue
            values = _parse_plugin_link(plugin, link)
            if values:
                existing = plugin_by_hash.setdefault(infohash, {})
                for key, value in values.items():
                    existing.setdefault(key, value)

    if not plugin_by_hash:
        return merged_by_type
    enriched_magnets = []
    for item in magnets:
        if not isinstance(item, dict):
            enriched_magnets.append(item)
            continue
        infohash = _magnet_infohash(item.get("url"))
        values = plugin_by_hash.get(infohash or "")
        if not values:
            enriched_magnets.append(item)
            continue
        enriched = dict(item)
        if not _has_structured_size(enriched.get("size")) and "size" in values:
            enriched["size"] = values["size"]
            enriched["size_source"] = "pansou"
        if not _has_structured_seeders(enriched.get("seeders")) and "seeders" in values:
            enriched["seeders"] = values["seeders"]
            enriched["seeders_source"] = "pansou"
            enriched["seeders_observed_at"] = observed_at
        enriched_magnets.append(enriched)
    return {**merged_by_type, "magnet": enriched_magnets}


def _parse_plugin_link(plugin: object, link: dict[str, Any]) -> dict[str, int]:
    if plugin == "plugin:nyaa":
        labels = ("大小", "做种")
        values = [
            link.get("content"),
            link.get("Content"),
            link.get("tags"),
            link.get("Tags"),
        ]
    elif plugin == "plugin:thepiratebay":
        labels = ("文件大小", "Seeders")
        values = [link.get("content"), link.get("Content")]
    else:
        return {}
    parsed: dict[str, int] = {}
    for value in values:
        if "size" not in parsed:
            size = _find_plugin_size(value, labels[0])
            if size is not None:
                parsed["size"] = size
        if "seeders" not in parsed:
            seeders = _find_plugin_seeders(value, labels[1])
            if seeders is not None:
                parsed["seeders"] = seeders
    return parsed


def _find_plugin_size(value: object, label: str) -> int | None:
    if isinstance(value, dict):
        direct = next(
            (
                item
                for key, item in value.items()
                if str(key).casefold() == label.casefold()
            ),
            None,
        )
        parsed = _parse_size_value(direct)
        if parsed is not None:
            return parsed
        for item in value.values():
            parsed = _find_plugin_size(item, label)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, list):
        for item in value:
            parsed = _find_plugin_size(item, label)
            if parsed is not None:
                return parsed
        return None
    if not isinstance(value, str):
        return None
    match = _PLUGIN_SIZE.search(value)
    if match is None or match.group("label").casefold() != label.casefold():
        return None
    return _parse_size_value(f"{match.group('number')} {match.group('unit')}")


def _find_plugin_seeders(value: object, label: str) -> int | None:
    if isinstance(value, dict):
        direct = next(
            (
                item
                for key, item in value.items()
                if str(key).casefold() == label.casefold()
            ),
            None,
        )
        parsed = _parse_seeders_value(direct)
        if parsed is not None:
            return parsed
        for item in value.values():
            parsed = _find_plugin_seeders(item, label)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, list):
        for item in value:
            parsed = _find_plugin_seeders(item, label)
            if parsed is not None:
                return parsed
        return None
    if not isinstance(value, str):
        return None
    match = _PLUGIN_SEEDERS.search(value)
    if match is None or match.group("label").casefold() != label.casefold():
        return None
    return int(match.group("number"))


def _parse_size_value(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        return int(float(match.group(1)) * _SIZE_MULTIPLIERS[match.group(2).upper()])
    except (KeyError, OverflowError, ValueError):
        return None


def _parse_seeders_value(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _has_structured_size(value: object) -> bool:
    if type(value) is int:
        return value >= 0
    return _parse_size_value(value) is not None


def _has_structured_seeders(value: object) -> bool:
    return _parse_seeders_value(value) is not None


def _magnet_infohash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() != "magnet":
        return None
    for key, raw in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() != "xt" or not raw.casefold().startswith("urn:btih:"):
            continue
        infohash = raw[9:]
        if re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            return infohash.casefold()
        if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", infohash):
            try:
                return base64.b32decode(infohash.upper()).hex()
            except (binascii.Error, ValueError):
                return None
    return None
