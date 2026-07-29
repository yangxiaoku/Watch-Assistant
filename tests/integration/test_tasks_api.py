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
from watch_assistant.models import Resource, Task, TaskState


class FakeTaskAdapter:
    async def submit_magnet(self, url: str):
        raise AssertionError("task adapter should not run in API tests")

    async def save_share(self, url: str, password: str | None):
        raise AssertionError("share adapter must not run in API tests")

    async def get_status(self, remote_ref: str):
        return None


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
        task_adapter=FakeTaskAdapter(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, app


@pytest.mark.integration
async def test_task_api_reuses_duplicate_and_rejects_arbitrary_url(tmp_path):
    client, database, tmdb, pansou, _app = await _make_task_client(tmp_path)

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


@pytest.mark.integration
async def test_retry_is_blocked_when_push_is_unsupported(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    created = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    task_id = created.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        task.state = TaskState.FAILED
        await session.commit()
    app.state.push_capabilities = {"magnet": False, "share": False}

    response = await client.post(f"/api/v1/tasks/{task_id}/retry")

    assert response.status_code == 503
    assert response.json()["detail"] == "push_kind_unsupported"
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
    assert task.state == TaskState.FAILED
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_task_creation_links_push_stage_to_workflow(tmp_path):
    client, database, tmdb, pansou, _app = await _make_task_client(tmp_path)

    workflow_response = await client.post(
        "/api/v1/workflows", json={"media_type": "movie", "tmdb_id": 27205}
    )
    assert workflow_response.status_code == 201
    workflow_id = workflow_response.json()["id"]

    task_response = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "workflow_id": workflow_id},
    )

    assert task_response.status_code == 202
    assert task_response.json()["workflow_id"] == workflow_id
    workflow = await client.get(f"/api/v1/workflows/{workflow_id}")
    push_stage = next(item for item in workflow.json()["stages"] if item["stage"] == "push")
    assert push_stage["status"] == "running"
    assert push_stage["child_type"] == "task"
    assert push_stage["child_id"] == task_response.json()["id"]

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()
