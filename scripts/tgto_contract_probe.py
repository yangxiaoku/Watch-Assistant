"""Discover TgtoDrive API path strings without invoking discovered routes."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable

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
UUID_SEGMENT = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
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


def _safe_static_prefix(
    candidate: str,
    forbidden_values: tuple[str, ...],
) -> str | None:
    safe_segments = []
    for segment in candidate.split("/")[2:]:
        proposed_path = "/api/" + "/".join([*safe_segments, segment])
        if (
            not SAFE_SEGMENT.fullmatch(segment)
            or OPAQUE_TOKEN.fullmatch(segment)
            or UUID_SEGMENT.fullmatch(segment)
            or any(value in proposed_path for value in forbidden_values)
        ):
            break
        safe_segments.append(segment)
        if segment.casefold() in CREDENTIAL_MARKERS:
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

        script_response = await client.get("/static/script.js")
        print(f"{script_response.status_code} /static/script.js")
        for path in extract_api_paths(
            script_response.text,
            forbidden_values=(username, password),
        ):
            print(path)


def main() -> None:
    asyncio.run(probe_routes())


if __name__ == "__main__":
    main()
