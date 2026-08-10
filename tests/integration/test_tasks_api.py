from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import RemoteObservation, RemoteStatus


class FakeTaskAdapter:
    def __init__(self):
        self.remote_status = None
        self.target_directory_ids = []

    async def submit_magnet(self, url: str):
        raise AssertionError("task adapter should not run in API tests")

    async def save_share(self, url: str, password: str | None):
        raise AssertionError("share adapter must not run in API tests")

    async def get_status_for_task(
        self, remote_ref: str, *, target_directory_id: str | None
    ):
        self.target_directory_ids.append(target_directory_id)
        return self.remote_status


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
    security_manager=make_security_manager(),
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
async def test_task_cancel_only_changes_unclaimed_task(tmp_path):
    client, database, tmdb, pansou, _app = await _make_task_client(tmp_path)
    created = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    task_id = created.json()["id"]

    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    repeated = await client.post(f"/api/v1/tasks/{task_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "task_not_cancellable"
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
    assert task.state == TaskState.CANCELLED
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

    # PushKindUnsupported 是能力冲突而非服务不可用:409 而非 503。
    assert response.status_code == 409
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
    discovery = await client.post(
        f"/api/v1/workflows/{workflow_id}/discovery",
        json={"resource_id": "res_task_api"},
    )
    assert discovery.status_code == 200
    for stage in ("inspection", "approval"):
        advanced = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/{stage}",
            json={"status": "succeeded"},
        )
        assert advanced.status_code == 200

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


@pytest.mark.integration
async def test_direct_push_skips_pending_prerequisite_stages(tmp_path):
    """A workflow created from a direct push must let the push stage start.

    Regression: creating a task against a fresh workflow previously failed
    with ``workflow_prerequisite_not_met`` because inspection/approval stayed
    PENDING. A direct push skips those stages instead of blocking.
    """
    client, database, tmdb, pansou, _app = await _make_task_client(tmp_path)

    workflow_response = await client.post(
        "/api/v1/workflows",
        json={"media_type": "movie", "tmdb_id": 27205, "resource_id": "res_task_api"},
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
    stage_status = {
        item["stage"]: item["status"] for item in workflow.json()["stages"]
    }
    assert stage_status["discovery"] == "succeeded"
    assert stage_status["inspection"] == "skipped"
    assert stage_status["approval"] == "skipped"
    assert stage_status["push"] == "running"

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_workflow_stage_patch_cannot_forge_discovery_or_availability(tmp_path):
    client, database, tmdb, pansou, _app = await _make_task_client(tmp_path)
    workflow = await client.post(
        "/api/v1/workflows", json={"media_type": "movie", "tmdb_id": 27205}
    )

    forged_discovery = await client.patch(
        f"/api/v1/workflows/{workflow.json()['id']}/stages/discovery",
        json={"status": "succeeded"},
    )
    forged_availability = await client.patch(
        f"/api/v1/workflows/{workflow.json()['id']}/stages/availability",
        json={"status": "succeeded"},
    )

    assert forged_discovery.status_code == 409
    assert forged_discovery.json()["error"]["code"] == "workflow_evidence_required"
    assert forged_availability.status_code == 409
    assert forged_availability.json()["error"]["code"] == "workflow_evidence_required"
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_uncertain_retry_requires_readonly_reconciliation(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    created = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    task_id = created.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.state = TaskState.UNCERTAIN
        task.remote_ref = "remote-uncertain"
        await session.commit()

    response = await client.post(f"/api/v1/tasks/{task_id}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "uncertain_requires_verification"
    reconciliation = await client.post(f"/api/v1/tasks/{task_id}/reconcile")
    assert reconciliation.status_code == 503
    assert reconciliation.json()["error"]["code"] == "reconciliation_unavailable"
    assert app.state.task_adapter.remote_status is None
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.state is TaskState.UNCERTAIN
        assert task.remote_ref == "remote-uncertain"
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_readonly_reconciliation_promotes_availability_and_records_evidence(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    workflow = await client.post(
        "/api/v1/workflows",
        json={"media_type": "movie", "tmdb_id": 27205, "resource_id": "res_task_api"},
    )
    workflow_id = workflow.json()["id"]
    for stage in ("inspection", "approval"):
        response = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/{stage}",
            json={"status": "succeeded"},
        )
        assert response.status_code == 200
    task_response = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "workflow_id": workflow_id},
    )
    task_id = task_response.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.state = TaskState.SUBMITTED
        task.remote_ref = "remote-available"
        await session.commit()
    app.state.task_adapter.remote_status = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="7",
        is_directory=False,
    )

    reconciled = await client.post(f"/api/v1/tasks/{task_id}/reconcile")
    workflow_after = await client.get(f"/api/v1/workflows/{workflow_id}")
    evidence = await client.get(f"/api/v1/tasks/{task_id}/evidence")

    assert reconciled.status_code == 200
    assert reconciled.json()["task"]["state"] == "available"
    assert reconciled.json()["task"]["state_zh"] == "文件已可用"
    assert reconciled.json()["evidence"]["source"] == "readonly_reconciliation"
    assert reconciled.json()["evidence"]["status"] == "available"
    assert reconciled.json()["evidence"]["verified"] is True
    stages = {item["stage"]: item["status"] for item in workflow_after.json()["stages"]}
    assert stages["push"] == "succeeded"
    assert stages["availability"] == "succeeded"
    assert stages["organization"] == "pending"
    assert evidence.status_code == 200
    assert evidence.json()[0]["status"] == "available"
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_bare_available_status_is_persisted_as_uncertain(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    created = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    task_id = created.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.state = TaskState.SUBMITTED
        task.remote_ref = "remote-bare-available"
        await session.commit()
    app.state.task_adapter.remote_status = RemoteStatus.AVAILABLE

    response = await client.post(f"/api/v1/tasks/{task_id}/reconcile")

    assert response.status_code == 200
    assert response.json()["task"]["state"] == "uncertain"
    assert response.json()["task"]["error_code"] == (
        "availability_observation_unverified"
    )
    assert response.json()["task"]["error_message"] == (
        "远端文件可用性尚未完成只读核验。"
    )
    assert response.json()["evidence"]["status"] == "uncertain"
    assert response.json()["evidence"]["verified"] is False
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_reconciliation_rejects_observation_outside_task_target(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    app.state.organization_target_root_id = "7"
    app.state.p115_browsed_directory_ids = {"7"}
    created = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "target_directory_id": "7"},
    )
    task_id = created.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.state = TaskState.SUBMITTED
        task.remote_ref = "remote-wrong-parent"
        await session.commit()
    app.state.task_adapter.remote_status = RemoteObservation(
        status=RemoteStatus.AVAILABLE,
        file_id="101",
        parent_id="8",
        is_directory=False,
    )

    response = await client.post(f"/api/v1/tasks/{task_id}/reconcile")

    assert response.status_code == 200
    assert response.json()["task"]["state"] == "uncertain"
    assert response.json()["task"]["error_code"] == "availability_parent_mismatch"
    assert response.json()["task"]["error_message"] == "远端文件父目录核验不一致。"
    assert response.json()["evidence"]["verified"] is False
    assert app.state.task_adapter.target_directory_ids == ["7"]
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_reconciliation_does_not_demote_available_task(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    created = await client.post("/api/v1/tasks", json={"resource_id": "res_task_api"})
    task_id = created.json()["id"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.state = TaskState.AVAILABLE
        task.remote_ref = "remote-available-terminal"
        await session.commit()
    app.state.task_adapter.remote_status = RemoteStatus.ACCEPTED

    response = await client.post(f"/api/v1/tasks/{task_id}/reconcile")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_stage_terminal"
    assert response.json()["error"]["message_zh"]
    async with database.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.state is TaskState.AVAILABLE
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_task_creation_persists_only_browsed_target_directory(tmp_path):
    client, database, tmdb, pansou, app = await _make_task_client(tmp_path)
    app.state.organization_target_root_id = "100"
    app.state.p115_browsed_directory_ids = {"100", "200"}

    selected = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "target_directory_id": "200"},
    )
    rejected = await client.post(
        "/api/v1/tasks",
        json={"resource_id": "res_task_api", "target_directory_id": "300"},
    )

    assert selected.status_code == 202
    assert selected.json()["target_directory_id"] == "200"
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "p115_directory_out_of_scope"
    async with database.session_factory() as session:
        stored = await session.get(Task, selected.json()["id"])
    assert stored is not None
    assert stored.target_directory_id == "200"
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()
