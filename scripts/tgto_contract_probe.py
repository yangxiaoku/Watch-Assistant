"""Discover TgtoDrive API path strings without invoking discovered routes."""

from __future__ import annotations

import asyncio
import os
import re

import httpx

API_PATH = re.compile(r"/api/[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@%/{}/-]*")
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
OPAQUE_TOKEN = re.compile(
    r"(?:"
    r"[A-Fa-f0-9]{24,}"
    r"|(?=[A-Za-z0-9_-]{24,}$)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9_-]+"
    r"|[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{8,})+"
    r")"
)


def _safe_static_prefix(candidate: str) -> str | None:
    safe_segments = []
    for segment in candidate.split("/")[2:]:
        if not SAFE_SEGMENT.fullmatch(segment) or OPAQUE_TOKEN.fullmatch(segment):
            break
        safe_segments.append(segment)
    if not safe_segments:
        return None
    return "/api/" + "/".join(safe_segments)


def extract_api_paths(script: str) -> list[str]:
    """Return unique API paths with query strings intentionally removed."""
    paths = {
        path
        for candidate in API_PATH.findall(script)
        if (path := _safe_static_prefix(candidate)) is not None
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

        script_response = await client.get("/static/script.js")
        print(f"{script_response.status_code} /static/script.js")
        for path in extract_api_paths(script_response.text):
            print(path)


def main() -> None:
    asyncio.run(probe_routes())


if __name__ == "__main__":
    main()
