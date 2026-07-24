"""Discover TgtoDrive API path strings without invoking discovered routes."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Iterable

import httpx

API_PATH = re.compile(r"/api/[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@%/{}/-]*")
CREDENTIAL_MARKERS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "password",
        "secret",
        "session",
        "token",
    }
)
KNOWN_STATIC_SEGMENTS = CREDENTIAL_MARKERS | {
    "1.0",
    "115",
    "115-share-strm-invalid-cleaner",
    "123-api-rate-limit",
    "ai-media-parser",
    "authorize",
    "bot-commands",
    "clear",
    "config",
    "connect",
    "continue",
    "dashboard",
    "dashboard_config",
    "deep_delete",
    "deep_delete_info",
    "delete",
    "delete-task-status",
    "delete_one",
    "detail",
    "directory-selector",
    "download",
    "emby",
    "emby-cover-generator",
    "emby-metadata-cleaner",
    "emby_proxy",
    "env",
    "fs",
    "gcid-export",
    "guangya",
    "hdhive",
    "history",
    "history-detail",
    "image",
    "input",
    "latest",
    "libraries",
    "list",
    "live",
    "login",
    "logs",
    "mac",
    "metadata",
    "notify-bots",
    "organize-history",
    "output",
    "preview",
    "proxy_settings",
    "pttransfer",
    "qr",
    "qrcode",
    "recognize-test",
    "records",
    "refresh",
    "refresh_cache",
    "reorganize",
    "reorganize-task-status",
    "resolve",
    "run",
    "save",
    "save_one",
    "search-tmdb",
    "send-code",
    "ssh",
    "status",
    "stop",
    "strm-sync",
    "subaccounts",
    "sync",
    "task-status",
    "tasks",
    "test",
    "tg-scheduled-sender",
    "tg-transfer",
    "tmdb",
    "tmdb-regex-rules",
    "toggle",
    "toolbox",
    "top_users_list",
    "transfer-history",
    "trend",
    "users",
    "validate",
    "verify-code",
    "visual-filter",
    "webhook",
}
KNOWN_ROOT_SEGMENTS = CREDENTIAL_MARKERS | {
    "1.0",
    "115",
    "115-share-strm-invalid-cleaner",
    "ai-media-parser",
    "bot-commands",
    "directory-selector",
    "emby",
    "emby-cover-generator",
    "emby-metadata-cleaner",
    "emby_proxy",
    "env",
    "guangya",
    "hdhive",
    "logs",
    "notify-bots",
    "organize-history",
    "proxy_settings",
    "pttransfer",
    "ssh",
    "status",
    "strm-sync",
    "tasks",
    "tg-transfer",
    "tmdb-regex-rules",
    "toolbox",
    "transfer-history",
    "visual-filter",
}
REDACTED_SEGMENT = "<redacted>"


def _safe_static_prefix(
    candidate: str,
    forbidden_values: tuple[str, ...],
) -> str | None:
    safe_segments = []
    segments = candidate.split("/")[2:]
    for index, segment in enumerate(segments):
        proposed_path = "/api/" + "/".join([*safe_segments, segment])
        if any(value in proposed_path for value in forbidden_values):
            safe_segments.append(REDACTED_SEGMENT)
            break
        normalized = segment.casefold()
        allowed_segments = KNOWN_ROOT_SEGMENTS if index == 0 else KNOWN_STATIC_SEGMENTS
        if normalized not in allowed_segments:
            safe_segments.append(REDACTED_SEGMENT)
            break
        safe_segments.append(normalized)
        if normalized in CREDENTIAL_MARKERS and index < len(segments) - 1:
            safe_segments.append(REDACTED_SEGMENT)
            break
    if not safe_segments:
        return None
    return "/api/" + "/".join(safe_segments)


def extract_api_paths(
    script: str,
    *,
    forbidden_values: Iterable[str] = (),
) -> list[str]:
    """Return unique API paths with query strings intentionally removed."""
    forbidden = tuple(value for value in forbidden_values if value)
    paths = {
        path
        for candidate in API_PATH.findall(script)
        if (path := _safe_static_prefix(candidate, forbidden)) is not None
    }
    return sorted(paths)


async def probe_routes() -> None:
    """Authenticate, fetch the static script, and print only status/path data."""
    base_url = os.environ.get("TGTO_BASE_URL")
    username = os.environ.get("TGTO_WEB_USER")
    password = os.environ.get("TGTO_WEB_PASSWORD")
    if not base_url or username is None or password is None:
        raise RuntimeError("required TgtoDrive probe configuration is unavailable")

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        login_response = await client.post(
            "/api/login",
            json={"username": username, "password": password},
        )
        print(f"{login_response.status_code} /api/login")
        login_response.raise_for_status()

        script_response = await client.get("/static/script.js")
        print(f"{script_response.status_code} /static/script.js")
        script_response.raise_for_status()
        for path in extract_api_paths(
            script_response.text,
            forbidden_values=(username, password),
        ):
            print(path)


def main() -> int:
    try:
        asyncio.run(probe_routes())
    except (httpx.HTTPError, RuntimeError, ValueError):
        print("ERROR /api/probe", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
