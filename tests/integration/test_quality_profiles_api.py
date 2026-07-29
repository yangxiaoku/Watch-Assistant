from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import QualityProfile, Resource


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'quality-api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_quality_api",
                kind="magnet",
                canonical_key="magnet:quality-api",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:quality-api"),
                name="Movie 2024 1080p WEB-DL HEVC",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=7),
                seeders=5,
            )
        )
        await session.commit()
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou


@pytest.mark.integration
async def test_quality_profile_api_conflict_unknown_rule_and_simulation_are_safe(
    tmp_path,
):
    client, database, tmdb, pansou = await _make_client(tmp_path)
    try:
        created = await client.post(
            "/api/v1/quality-profiles",
            json={
                "name": "API profile",
                "rules": {"hard": {"min_resolution": 1080}},
            },
        )
        assert created.status_code == 201
        profile_id = created.json()["id"]
        revision = created.json()["revision"]

        listed = await client.get("/api/v1/quality-profiles")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [profile_id]

        unknown = await client.patch(
            f"/api/v1/quality-profiles/{profile_id}",
            json={"revision": revision, "rules": {"hard": {"typo": True}}},
        )
        assert unknown.status_code == 422
        assert unknown.json()["detail"] == "unknown_quality_rule"

        conflict = await client.patch(
            f"/api/v1/quality-profiles/{profile_id}",
            json={"revision": revision + 1, "name": "stale"},
        )
        assert conflict.status_code == 409

        simulated = await client.post(
            f"/api/v1/quality-profiles/{profile_id}/simulate",
            json={"resource_ids": ["res_quality_api"]},
        )
        assert simulated.status_code == 200
        assert simulated.json()["items"][0]["eligible"] is True
        assert "magnet:?" not in simulated.text

        async with database.session_factory() as session:
            profiles = list(await session.scalars(select(QualityProfile)))
        assert len(profiles) == 1
        assert profiles[0].revision == revision
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_quality_profile_api_requires_auth_when_security_is_configured(
    tmp_path,
):
    from watch_assistant.security import SecurityManager

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=SecurityManager(
            web_password_hash="invalid", script_token_hash="invalid"
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    try:
            response = await client.get("/api/v1/quality-profiles")
            assert response.status_code == 401
            assert response.json()["detail"] == "unauthorized"
            assert response.json()["error"]["code"] == "unauthorized"
            assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
