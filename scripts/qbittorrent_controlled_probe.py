"""Run one redacted qBittorrent metadata probe against a controlled torrent URL."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx


def _secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError("credential_missing")
    return value


async def _run(args: argparse.Namespace) -> dict[str, object]:
    marker = f"wa-controlled-probe-{uuid.uuid4().hex}"
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/")) as client:
        login = await client.post(
            "/api/v2/auth/login",
            data={"username": _secret(args.username_path), "password": _secret(args.password_path)},
            timeout=args.request_timeout,
        )
        if login.status_code >= 400 or login.text.strip() not in {"Ok.", ""}:
            return {"status": "blocked", "error_code": "authentication_failed", "cleanup": "not_started"}
        version_response = await client.get("/api/v2/app/version", timeout=args.request_timeout)
        version_response.raise_for_status()
        info_response = await client.get("/api/v2/transfer/info", timeout=args.request_timeout)
        info_response.raise_for_status()
        info = info_response.json()
        version = version_response.text.strip()
        dht_nodes = info.get("dht_nodes") if isinstance(info, dict) else None
        connection_status = info.get("connection_status") if isinstance(info, dict) else None
        added_hash: str | None = None
        result: dict[str, object] = {
            "status": "failed",
            "error_code": "probe_failed",
            "cleanup": "not_started",
            "version": version,
            "dht_nodes": dht_nodes,
            "connection_status": connection_status,
        }
        try:
            response = await client.post(
                "/api/v2/torrents/add",
                data={
                    "urls": args.torrent_url,
                    "category": marker,
                    "tags": marker,
                    "stopCondition": "MetadataReceived",
                },
                timeout=args.request_timeout,
            )
            if response.status_code >= 400 or response.text.strip() == "Fails.":
                result.update({"status": "unsupported", "error_code": "metadata_stop_unsupported"})
                return result
            deadline = time.monotonic() + args.item_timeout
            while time.monotonic() < deadline:
                listing = await client.get(
                    "/api/v2/torrents/info",
                    params={"tag": marker},
                    timeout=args.request_timeout,
                )
                listing.raise_for_status()
                payload = listing.json()
                rows = payload if isinstance(payload, list) else []
                row = rows[0] if rows else None
                if isinstance(row, dict) and isinstance(row.get("hash"), str):
                    added_hash = row["hash"]
                    if row.get("has_metadata") is True:
                        files = await client.get(
                            "/api/v2/torrents/files",
                            params={"hash": added_hash},
                            timeout=args.request_timeout,
                        )
                        files.raise_for_status()
                        file_rows = files.json()
                        valid_files = [
                            item
                            for item in file_rows
                            if isinstance(item, dict) and isinstance(item.get("size"), int)
                        ] if isinstance(file_rows, list) else []
                        result.update(
                            {
                                "status": "verified",
                                "error_code": None,
                                "file_count": len(valid_files),
                                "total_size_bytes": sum(item["size"] for item in valid_files),
                            }
                        )
                        return result
                await asyncio.sleep(min(args.poll_interval, max(0.0, deadline - time.monotonic())))
            result.update({"status": "timeout", "error_code": "metadata_timeout"})
            return result
        finally:
            try:
                hashes = [added_hash] if added_hash is not None else []
                if not hashes:
                    marker_listing = await client.get(
                        "/api/v2/torrents/info",
                        params={"tag": marker},
                        timeout=args.request_timeout,
                    )
                    marker_listing.raise_for_status()
                    rows = marker_listing.json()
                    hashes = [
                        row["hash"]
                        for row in (rows if isinstance(rows, list) else [])
                        if isinstance(row, dict) and isinstance(row.get("hash"), str)
                    ]
                if hashes:
                    cleanup = await client.post(
                        "/api/v2/torrents/delete",
                        data={"hashes": "|".join(hashes), "deleteFiles": "true"},
                        timeout=args.request_timeout,
                    )
                    result["cleanup"] = (
                        "complete" if cleanup.status_code < 400 else "failed"
                    )
                else:
                    result["cleanup"] = "complete"
            except (httpx.HTTPError, ValueError):
                result["cleanup"] = "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded qBittorrent controlled metadata probe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username-path", required=True, type=Path)
    parser.add_argument("--password-path", required=True, type=Path)
    parser.add_argument("--torrent-url", required=True)
    parser.add_argument("--item-timeout", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    args = parser.parse_args()
    if not 1 <= args.item_timeout <= 120:
        parser.error("item-timeout must be between 1 and 120 seconds")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
