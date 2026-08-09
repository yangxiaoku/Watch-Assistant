from datetime import UTC, datetime

import pytest

from tests.unit.factories import make_task
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import (
    Resource,
    Task,
    TaskState,
    Workflow,
    WorkflowStage,
)
from watch_assistant.schemas import (
    MediaType,
    WorkflowStageName,
    WorkflowStageStatus,
    WorkflowStatus,
)
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.workflows import WorkflowService


@pytest.mark.asyncio
async def test_retry_failed_batch_retries_only_failed_tasks(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            resources = [
                Resource(
                    id=f"res-{index}",
                    kind="magnet",
                    canonical_key=f"magnet:test-{index}",
                    encrypted_url="encrypted",
                    name="Test",
                    source="test",
                    captured_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC),
                )
                for index in range(3)
            ]
            session.add_all(resources)
            await session.flush()
            failed = [
                make_task(state=TaskState.FAILED, resource_id="res-0", age_hours=2),
                make_task(state=TaskState.FAILED, resource_id="res-1", age_hours=4),
            ]
            uncertain = make_task(
                state=TaskState.UNCERTAIN, resource_id="res-2"
            )
            session.add_all([*failed, uncertain])
            await session.commit()

        service = TaskService(database.session_factory)
        result = await service.retry_failed_batch(limit=10)
        assert result["requested"] == 2
        assert result["retried"] == 2
        assert result["failed"] == []

        async with database.session_factory() as session:
            rows = list(
                (await session.scalars(
                    __import__("sqlalchemy").select(Task)
                )).all()
            )
        states = {task.state for task in rows}
        assert TaskState.QUEUED in states  # retried tasks are re-queued
        assert TaskState.UNCERTAIN in states  # uncertain is untouched
        assert TaskState.FAILED not in states
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_cancellable_batch_stops_only_pending_stages(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            cancellable = Workflow(
                id="wf_batch_cancel_1",
                correlation_id="corr_1",
                media_type=MediaType.MOVIE,
                tmdb_id=1,
                status=WorkflowStatus.IN_PROGRESS,
                state_reason="workflow_created",
            )
            running = Workflow(
                id="wf_batch_running_1",
                correlation_id="corr_2",
                media_type=MediaType.MOVIE,
                tmdb_id=2,
                status=WorkflowStatus.RESULT_PENDING_CONFIRMATION,
                state_reason="workflow_created",
            )
            session.add_all([cancellable, running])
            session.add_all([
                WorkflowStage(
                    id="stage_pending_1",
                    workflow_id=cancellable.id,
                    stage=WorkflowStageName.APPROVAL,
                    status=WorkflowStageStatus.PENDING,
                ),
                WorkflowStage(
                    id="stage_running_1",
                    workflow_id=running.id,
                    stage=WorkflowStageName.INSPECTION,
                    status=WorkflowStageStatus.UNCERTAIN,
                ),
            ])
            await session.commit()

        service = WorkflowService(database.session_factory)
        result = await service.cancel_cancellable_batch(limit=10, actor_id="test")
        assert result["cancelled"] == 1
        assert result["requested"] == 2
        # The running/uncertain workflow has no cancellable stage and is skipped.
        assert any(
            failure["workflow_id"] == "wf_batch_running_1"
            for failure in result["failed"]
        )
    finally:
        await database.engine.dispose()
