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
from watch_assistant.models import Resource, Task, WorkflowStage
from watch_assistant.schemas import RemoteStatus, WorkflowStageStatus
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.workflows import WorkflowService


class FakeTaskAdapter:
    async def submit_magnet(self, url: str):
        raise AssertionError("workflow API must not submit during association")

    async def save_share(self, url: str, password: str | None):
        raise AssertionError("workflow API must not submit during association")

    async def get_status(self, remote_ref: str):
        return None


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'workflow-api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_workflow_api",
                kind="magnet",
                canonical_key="magnet:workflow-api",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:workflow-api"),
                name="Workflow Movie",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=7),
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
    return client, database, tmdb, pansou


async def _record_discovery(client, workflow_id: str):
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/discovery",
        json={"resource_id": "res_workflow_api"},
    )
    assert response.status_code == 200
    return response


@pytest.mark.integration
async def test_workflow_timeline_aggregates_stage_state_and_child_task(tmp_path):
    client, database, tmdb, pansou = await _make_client(tmp_path)
    try:
        created = await client.post(
            "/api/v1/workflows",
            json={"media_type": "movie", "tmdb_id": 7, "subscription_id": "sub_demo"},
        )
        assert created.status_code == 201
        workflow = created.json()
        workflow_id = workflow["id"]
        assert workflow["correlation_id"].startswith("corr_")
        assert len(workflow["stages"]) == 7
        assert [stage["sequence"] for stage in workflow["stages"]] == list(range(7))
        assert "magnet:?" not in created.text

        discovery = await _record_discovery(client, workflow_id)
        assert discovery.json()["status"] == "in_progress"

        for stage in ("inspection", "approval"):
            advanced = await client.patch(
                f"/api/v1/workflows/{workflow_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
            assert advanced.status_code == 200

        task = await client.post(
            "/api/v1/tasks",
            json={"resource_id": "res_workflow_api", "workflow_id": workflow_id},
        )
        assert task.status_code == 202

        detail = await client.get(f"/api/v1/workflows/{workflow_id}")
        assert detail.status_code == 200
        push = next(item for item in detail.json()["stages"] if item["stage"] == "push")
        assert push["child_type"] == "task"
        assert push["child_id"] == task.json()["id"]

        filtered = await client.get(
            "/api/v1/workflows", params={"status": "in_progress", "page_size": 10}
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["id"] == workflow_id

        by_subscription = await client.get(
            "/api/v1/workflows", params={"subscription_id": "sub_demo"}
        )
        assert by_subscription.status_code == 200
        assert by_subscription.json()["total"] == 1

        by_stage = await client.get(
            "/api/v1/workflows",
            params={"stage": "push", "stage_status": "running"},
        )
        assert by_stage.status_code == 200
        assert by_stage.json()["total"] == 1

        invalid_stage_status = await client.get(
            "/api/v1/workflows", params={"stage_status": "not-a-stage-status"}
        )
        assert invalid_stage_status.status_code == 422
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_workflow_stage_order_and_uncertain_priority(tmp_path):
    client, database, tmdb, pansou = await _make_client(tmp_path)
    try:
        created = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        workflow_id = created.json()["id"]
        await _record_discovery(client, workflow_id)
        for stage in ("inspection", "approval"):
            response = await client.patch(
                f"/api/v1/workflows/{workflow_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
            assert response.status_code == 200

        uncertain = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/push",
            json={"status": "uncertain", "error_code": "remote_status_unknown"},
        )
        assert uncertain.status_code == 200
        assert uncertain.json()["status"] == "result_pending_confirmation"
        assert uncertain.json()["status_zh"] == "结果待确认"

        terminal = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/push",
            json={"status": "running"},
        )
        assert terminal.status_code == 409
        assert terminal.json()["error"]["code"] == "workflow_stage_regression"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_workflow_approval_and_cancel_are_guarded(tmp_path):
    client, database, tmdb, pansou = await _make_client(tmp_path)
    try:
        awaiting = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        workflow_id = awaiting.json()["id"]
        await _record_discovery(client, workflow_id)
        for stage in ("inspection",):
            advanced = await client.patch(
                f"/api/v1/workflows/{workflow_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
            assert advanced.status_code == 200
        waiting = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/approval",
            json={"status": "waiting_confirmation", "reason": "需要用户选择"},
        )
        assert waiting.status_code == 200
        approved = await client.post(
            f"/api/v1/workflows/{workflow_id}/approval",
            json={"decision": "approve", "reason": "用户确认"},
        )
        assert approved.status_code == 200
        approval_stage = next(
            item for item in approved.json()["stages"] if item["stage"] == "approval"
        )
        assert approval_stage["status"] == "succeeded"
        assert approved.json()["status"] == "in_progress"

        rejected = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        rejected_id = rejected.json()["id"]
        await _record_discovery(client, rejected_id)
        for stage in ("inspection",):
            advanced = await client.patch(
                f"/api/v1/workflows/{rejected_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
            assert advanced.status_code == 200
        await client.patch(
            f"/api/v1/workflows/{rejected_id}/stages/approval",
            json={"status": "waiting_confirmation"},
        )
        rejected_result = await client.post(
            f"/api/v1/workflows/{rejected_id}/approval",
            json={"decision": "reject"},
        )
        assert rejected_result.status_code == 200
        assert rejected_result.json()["status"] == "partial"
        rejected_stages = {
            stage["stage"]: stage["status"]
            for stage in rejected_result.json()["stages"]
        }
        assert rejected_stages["discovery"] == "succeeded"
        assert rejected_stages["inspection"] == "succeeded"
        assert rejected_stages["approval"] == "cancelled"
        assert rejected_stages["push"] == "cancelled"

        cancellable = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        cancelled = await client.post(
            f"/api/v1/workflows/{cancellable.json()['id']}/cancel",
            json={"reason": "用户取消"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        running = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        running_id = running.json()["id"]
        await _record_discovery(client, running_id)
        for stage in ("inspection", "approval", "push"):
            advanced = await client.patch(
                f"/api/v1/workflows/{running_id}/stages/{stage}",
                json={"status": "succeeded" if stage != "push" else "running"},
            )
            assert advanced.status_code == 200
        partially_cancelled = await client.post(
            f"/api/v1/workflows/{running_id}/cancel", json={}
        )
        assert partially_cancelled.status_code == 200
        assert partially_cancelled.json()["status"] == "in_progress"
        stages = {
            item["stage"]: item["status"]
            for item in partially_cancelled.json()["stages"]
        }
        assert stages["push"] == "running"
        assert stages["discovery"] == "succeeded"
        assert stages["inspection"] == "succeeded"
        assert stages["approval"] == "succeeded"
        assert stages["availability"] == "cancelled"
        assert stages["strm"] == "cancelled"

        no_pending = await client.post(
            "/api/v1/workflows", json={"media_type": "movie"}
        )
        no_pending_id = no_pending.json()["id"]
        await _record_discovery(client, no_pending_id)
        for stage in ("inspection", "approval"):
            await client.patch(
                f"/api/v1/workflows/{no_pending_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
        task, _ = await TaskService(database.session_factory).create(
            "res_workflow_api", workflow_id=no_pending_id
        )
        async with database.session_factory() as session:
            stored_task = await session.get(Task, task.id)
            assert stored_task is not None
            stored_task.remote_ref = "remote-available"
            await session.commit()

        class AvailableAdapter:
            async def get_status(self, remote_ref: str):
                assert remote_ref == "remote-available"
                return RemoteStatus.AVAILABLE

        await TaskService(database.session_factory).reconcile(
            task.id, AvailableAdapter()
        )
        for stage in ("organization", "strm"):
            await client.patch(
                f"/api/v1/workflows/{no_pending_id}/stages/{stage}",
                json={"status": "succeeded"},
            )
        blocked = await client.post(f"/api/v1/workflows/{no_pending_id}/cancel", json={})
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "workflow_not_cancellable"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_workflow_rejects_late_stage_and_recovers_stale_child(tmp_path):
    client, database, tmdb, pansou = await _make_client(tmp_path)
    try:
        created = await client.post("/api/v1/workflows", json={"media_type": "movie"})
        workflow_id = created.json()["id"]
        late = await client.patch(
            f"/api/v1/workflows/{workflow_id}/stages/push",
            json={"status": "succeeded"},
        )
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "workflow_prerequisite_not_met"

        async with database.session_factory() as session:
            stage = await session.scalar(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_id == workflow_id,
                    WorkflowStage.stage == "discovery",
                )
            )
            assert stage is not None
            stage.status = WorkflowStageStatus.RUNNING
            stage.child_id = "child-restart"
            stage.updated_at = datetime.now(UTC) - timedelta(days=2)
            await session.commit()

        recovered = await WorkflowService(database.session_factory).recover_stale()
        assert recovered == 1
        detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
        discovery = next(item for item in detail["stages"] if item["stage"] == "discovery")
        assert discovery["status"] == "uncertain"
        assert discovery["reason_zh"] == "阶段长时间没有更新，已暂停并等待核对。"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
