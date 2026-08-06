import secrets
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.prowlarr import ProwlarrClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import ApplicationSettings


def _fixture_hostname_resolver(_hostname: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings-api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    prowlarr = ProwlarrClient("http://prowlarr.test", secrets.token_urlsafe(24))
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        prowlarr_client=prowlarr,
        prowlarr_hostname_resolver=_fixture_hostname_resolver,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, prowlarr


@pytest.mark.integration
@respx.mock
async def test_prowlarr_settings_never_echo_key_and_verify_is_read_only(
    tmp_path: Path,
):
    api_key = secrets.token_urlsafe(24)
    route = respx.get("http://prowlarr.test/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path)
    try:
        saved = await client.patch(
            "/api/v1/settings/search-sources/prowlarr",
            json={
                "enabled": True,
                "base_url": "http://prowlarr.test/",
                "api_key": api_key,
                "revision": 0,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["base_url"] == "http://prowlarr.test"
        assert saved.json()["api_key_source"] == "managed"
        assert api_key not in saved.text

        current = await client.get("/api/v1/settings/search-sources/prowlarr")
        assert current.status_code == 200
        assert current.json()["revision"] == 1
        assert api_key not in current.text

        sources = await client.get("/api/v1/settings/search-sources")
        assert sources.status_code == 200
        assert sources.json()["pansou"]["status"] == "configured"
        assert sources.json()["prowlarr"]["status"] == "configured"
        assert sources.json()["prowlarr"]["state"] == "unverified"
        assert sources.json()["prowlarr"]["reason_code"] == "prowlarr_unverified"

        verified = await client.post(
            "/api/v1/settings/search-sources/prowlarr/verify"
        )
        assert verified.status_code == 200
        assert verified.json()["status"] == "available"
        assert verified.json()["state"] == "available"
        assert verified.json()["reason_code"] == "prowlarr_available"
        assert route.calls[-1].request.headers["X-Api-Key"] == api_key
        assert api_key not in str(route.calls[-1].request.url)

        async with database.session_factory() as session:
            stored = await session.get(ApplicationSettings, "default")
        assert stored is not None
        assert stored.managed_prowlarr_api_key_encrypted
        assert api_key not in stored.managed_prowlarr_api_key_encrypted

        reset = await client.post(
            "/api/v1/settings/search-sources/prowlarr/reset",
            json={"revision": 1},
        )
        assert reset.status_code == 200
        assert reset.json()["configured"] is False
        sources = await client.get("/api/v1/settings/search-sources")
        assert sources.json()["prowlarr"]["status"] == "disabled"
        assert sources.json()["prowlarr"]["state"] == "disabled"
        assert sources.json()["prowlarr"]["reason_code"] == "prowlarr_disabled"

        stale = await client.patch(
            "/api/v1/settings/search-sources/prowlarr",
            json={"revision": 1, "enabled": True},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "settings_conflict"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await prowlarr.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_invalid_prowlarr_url_uses_safe_chinese_error_mapping(tmp_path):
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path)
    try:
        response = await client.patch(
            "/api/v1/settings/search-sources/prowlarr",
            json={
                "enabled": True,
                "base_url": "https://user:secret@127.0.0.1/?token=hidden",
                "api_key": "fixture-only",
                "revision": 0,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "invalid_prowlarr_settings"
        assert response.json()["error"]["message_zh"]
        assert "https://user:secret@127.0.0.1" not in response.text
        assert "user:secret" not in response.text
        assert "token=hidden" not in response.text

        async with database.session_factory() as session:
            stored = await session.get(ApplicationSettings, "default")
        assert stored is not None
        assert stored.revision == 0
        assert stored.managed_prowlarr_base_url is None
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await prowlarr.aclose()
        await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status_code", "headers", "expected_state", "expected_reason"),
    [
        (429, {"Retry-After": "42"}, "backoff", "prowlarr_rate_limited"),
        (500, {}, "degraded", "prowlarr_server_error"),
    ],
)
@respx.mock
async def test_prowlarr_verify_returns_safe_health_reason_and_retry_window(
    tmp_path: Path,
    status_code: int,
    headers: dict[str, str],
    expected_state: str,
    expected_reason: str,
):
    route = respx.get("http://prowlarr.test/api/v1/search").mock(
        return_value=httpx.Response(
            status_code,
            json={"error": "fixture_upstream_detail"},
            headers=headers,
        )
    )
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path)
    api_key = "fixture-only"
    try:
        saved = await client.patch(
            "/api/v1/settings/search-sources/prowlarr",
            json={
                "enabled": True,
                "base_url": "http://prowlarr.test",
                "api_key": api_key,
                "revision": 0,
            },
        )
        assert saved.status_code == 200

        verified = await client.post(
            "/api/v1/settings/search-sources/prowlarr/verify"
        )
        assert verified.status_code == 200
        body = verified.json()
        assert body["status"] == "unavailable"
        assert body["state"] == expected_state
        assert body["reason_code"] == expected_reason
        assert body["retry_after_seconds"] is not None
        assert "fixture_upstream_detail" not in verified.text
        assert api_key not in verified.text

        sources = await client.get("/api/v1/settings/search-sources")
        source = sources.json()["prowlarr"]
        assert source["state"] == expected_state
        assert source["reason_code"] == expected_reason
        assert source["retry_after_seconds"] is not None
        assert "fixture_upstream_detail" not in sources.text
        assert route.calls
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await prowlarr.aclose()
        await database.engine.dispose()
