from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource


async def _make_task_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'tasks-api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_task_api",
                kind="magnet",
                canonical_key="magnet:task-api",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:task-api"),
                name="Movie",
                source="PanSou",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
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
async def test_task_api_reuses_duplicate_and_rejects_arbitrary_url(tmp_path):
    client, database, tmdb, pansou = await _make_task_client(tmp_path)

    first = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    second = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    invalid = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "url": "magnet:?xt=urn:btih:inject"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert invalid.status_code == 422
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()
