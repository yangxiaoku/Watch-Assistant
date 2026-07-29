"""Run a bounded, redacted PanSou and qBittorrent acceptance probe."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.qbittorrent import QbittorrentClient, _extract_infohash


def _read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("credential_missing")
    return value


def _magnet_urls(payload: dict[str, Any], limit: int) -> tuple[list[str], Counter[str]]:
    data = payload.get("merged_by_type")
    if not isinstance(data, dict):
        raise TypeError("pansou_shape_invalid")
    candidates: list[tuple[int, str, str]] = []
    sources: Counter[str] = Counter()
    for items in data.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("magnet:"):
                continue
            source = str(item.get("source", "unknown"))
            sources[source] += 1
            tracker_count = len(parse_qs(urlsplit(url).query).get("tr", []))
            candidates.append((tracker_count, source, url))
    unique: list[str] = []
    seen: set[str] = set()
    for _, _, url in sorted(candidates, key=lambda item: (-item[0], item[1])):
        infohash = _extract_infohash(url)
        if infohash is None:
            continue
        if infohash in seen:
            continue
        seen.add(infohash)
        unique.append(url)
        if len(unique) == limit:
            break
    return unique, sources


async def _run(args: argparse.Namespace) -> dict[str, object]:
    pansou = PanSouClient(args.pansou_url, timeout=args.timeout)
    qb = QbittorrentClient(
        args.qb_url,
        _read_secret(args.qb_username_path),
        _read_secret(args.qb_password_path),
        concurrency=args.concurrency,
        item_timeout=args.item_timeout,
        poll_interval=args.poll_interval,
        request_timeout=args.timeout,
    )
    try:
        payload = await pansou.search(args.keyword)
        magnets, source_counts = _magnet_urls(payload, args.sample_size)
        await qb.ensure_available()
        preferences = await qb._client.get("/api/v2/app/preferences", timeout=args.timeout)
        preferences.raise_for_status()
        preference_body = preferences.json()
        results = await qb.inspect(magnets)
        statuses = Counter(item.status.value for item in results)
        error_codes = Counter(
            item.error_code for item in results if item.error_code is not None
        )
        return {
            "pansou": {
                "total": payload.get("total"),
                "magnet_count": sum(source_counts.values()),
                "sample_count": len(magnets),
                "source_counts": dict(sorted(source_counts.items())),
            },
            "qbittorrent": {
                "ready": True,
                "dht_enabled": (
                    preference_body.get("dht")
                    if isinstance(preference_body, dict)
                    else None
                ),
                "sample_count": len(results),
                "statuses": dict(sorted(statuses.items())),
                "error_codes": dict(sorted(error_codes.items())),
            },
        }
    finally:
        await qb.aclose()
        await pansou.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded live external acceptance")
    parser.add_argument("--pansou-url", required=True)
    parser.add_argument("--qb-url", required=True)
    parser.add_argument("--qb-username-path", required=True)
    parser.add_argument("--qb-password-path", required=True)
    parser.add_argument("--keyword", default="盗梦空间 2010")
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--item-timeout", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 10:
        parser.error("sample-size must be between 1 and 10")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
