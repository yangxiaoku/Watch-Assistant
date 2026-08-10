from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource

TMDB_RESPONSE = {
    "id": 12345,
    "title": "盗梦空间",
    "original_title": "Inception",
    "release_date": "2010-07-16",
    "overview": "Dreams within dreams.",
}
MAGNET = (
    "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
    "&dn=Inception%202010%201080p"
)


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'import.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        share_domains=("115.com",),
    security_manager=make_security_manager(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, crypto


@pytest.mark.integration
@respx.mock
async def test_manual_import_is_preview_first_encrypted_and_deduplicated(tmp_path):
    respx.get("https://api.themoviedb.org/3/movie/12345").mock(
        return_value=httpx.Response(200, json=TMDB_RESPONSE)
    )
    client, database, tmdb, pansou, crypto = await _make_client(tmp_path)
    try:
        payload = {
            "url": MAGNET,
            "password": "share-secret",
            "tmdb_id": 12345,
            "name": "Inception 2010 1080p",
        }
        preview = await client.post("/api/v1/imports/preview", json=payload)
        assert preview.status_code == 200
        assert preview.json()["status"] == "ready"
        assert MAGNET not in preview.text
        assert "share-secret" not in preview.text

        unconfirmed = await client.post("/api/v1/imports", json=payload)
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"] == "confirmation_required"

        created = await client.post(
            "/api/v1/imports", json={**payload, "confirmed": True}
        )
        assert created.status_code == 201
        assert created.json()["status"] == "created"
        assert created.json()["task_created"] is False
        assert MAGNET not in created.text
        assert "share-secret" not in created.text

        async with database.session_factory() as session:
            resources = list(await session.scalars(select(Resource)))
        assert len(resources) == 1
        assert resources[0].source == "manual"
        assert crypto.decrypt(resources[0].encrypted_url) == MAGNET
        assert crypto.decrypt(resources[0].encrypted_password) == "share-secret"

        duplicate = await client.post("/api/v1/imports/preview", json=payload)
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
@respx.mock
async def test_manual_import_without_name_uses_magnet_dn_as_display_name(tmp_path):
    """A name-less magnet import must use its dn as the display name, not the
    placeholder note, or every such import is rejected as media_mismatch."""
    respx.get("https://api.themoviedb.org/3/movie/12345").mock(
        return_value=httpx.Response(200, json=TMDB_RESPONSE)
    )
    client, database, tmdb, pansou, _crypto = await _make_client(tmp_path)
    try:
        preview = await client.post(
            "/api/v1/imports/preview",
            json={"url": MAGNET, "tmdb_id": 12345},
        )
        assert preview.status_code == 200
        assert preview.json()["status"] == "ready"
        assert preview.json()["name"] == "Inception 2010 1080p"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
@respx.mock
async def test_manual_import_rejects_arbitrary_url_and_media_mismatch(tmp_path):
    respx.get("https://api.themoviedb.org/3/movie/12345").mock(
        return_value=httpx.Response(200, json=TMDB_RESPONSE)
    )
    client, database, tmdb, pansou, _crypto = await _make_client(tmp_path)
    try:
        arbitrary = await client.post(
            "/api/v1/imports/preview",
            json={
                "url": "https://example.com/file.torrent",
                "tmdb_id": 12345,
                "name": "Inception 2010 1080p",
            },
        )
        assert arbitrary.status_code == 422
        assert arbitrary.json()["detail"] == "unsupported_share_domain"

        mismatch = await client.post(
            "/api/v1/imports/preview",
            json={
                "url": MAGNET,
                "tmdb_id": 12345,
                "name": "Different Movie 2024 1080p",
            },
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"] == "media_mismatch"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
