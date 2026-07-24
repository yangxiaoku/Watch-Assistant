"""Discover TgtoDrive API path strings without invoking discovered routes."""

from __future__ import annotations

import asyncio
import os
import re

import httpx

API_PATH = re.compile(r"/api/[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@%/{}/-]*")


def extract_api_paths(script: str) -> list[str]:
    """Return unique API paths with query strings intentionally removed."""
    return sorted(set(API_PATH.findall(script)))


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
