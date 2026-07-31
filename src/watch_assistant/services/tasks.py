"""Task creation, idempotency, and state transitions."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Resource, Task, TaskState
from watch_assistant.schemas import (
    RemoteStatus,
    TaskAction,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.workflows import (
    emit_workflow_stage_changed,
    link_child,
    sync_child_stage,
)

REUSABLE_STATES = (TaskState.QUEUED, TaskState.SUBMITTING, TaskState.ACCEPTED)


class ResourceNotFound(LookupError):
    pass


class InvalidRetryState(ValueError):
    pass


class InvalidCancelState(ValueError):
    pass


class PushKindUnsupported(ValueError):
    pass


def recover_after_restart(task: Task, remote_status: RemoteStatus | None) -> None:
    task.lease_owner = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)
    task.state = (
        TaskState(remote_status.value)
        if remote_status is not None
        else TaskState.UNCERTAIN
    )


def choose_existing_task(
    tasks, resource_id: str, target_directory_id: str | None = None
) -> Task | None:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    candidates = [
        task
        for task in tasks
        if task.resource_id == resource_id
        and task.target_directory_id == target_directory_id
        and task.state in REUSABLE_STATES
        and _as_utc(task.created_at) >= cutoff
    ]
    return max(candidates, key=lambda task: _as_utc(task.created_at), default=None)


def prepare_manual_retry(task: Task) -> None:
    if task.state not in {
        TaskState.FAILED,
        TaskState.NEEDS_AUTH,
        TaskState.UNCERTAIN,
    }:
        raise InvalidRetryState("task is not retryable")
    task.state = TaskState.QUEUED
    task.remote_ref = None
    task.error_code = None
    task.error_message = None
    task.lease_owner = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)


class TaskService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._create_lock = asyncio.Lock()
        self._event_logger = event_logger

    async def create(
        self,
        resource_id: str,
        *,
        force: bool = False,
        allowed_actions: frozenset[TaskAction] | None = None,
        workflow_id: str | None = None,
        target_directory_id: str | None = None,
    ):
        async with self._create_lock, self._session_factory() as session:
            stage_workflow = None
            resource = await session.get(Resource, resource_id)
            if resource is None:
                raise ResourceNotFound(resource_id)
            action = (
                TaskAction.OFFLINE_DOWNLOAD
                if resource.kind.value == "magnet"
                else TaskAction.SAVE_SHARE
            )
            if allowed_actions is not None and action not in allowed_actions:
                raise PushKindUnsupported("push kind is not supported")
            if not force:
                existing_tasks = await session.scalars(
                    select(Task).where(Task.resource_id == resource_id)
                )
                existing = choose_existing_task(
                    existing_tasks, resource_id, target_directory_id
                )
                if existing is not None:
                    if workflow_id is not None and existing.workflow_id is None:
                        existing.workflow_id = workflow_id
                        stage_workflow = await link_child(
                            session,
                            workflow_id,
                            WorkflowStageName.PUSH,
                            "task",
                            existing.id,
                        )
                        await session.commit()
                        await emit_workflow_stage_changed(
                            self._event_logger,
                            workflow_id=stage_workflow.id,
                            correlation_id=stage_workflow.correlation_id,
                            stage_name=WorkflowStageName.PUSH,
                            status=WorkflowStageStatus.RUNNING,
                        )
                    return existing, True

            task = Task(
                id="task_" + uuid4().hex,
                workflow_id=workflow_id,
                target_directory_id=target_directory_id,
                resource_id=resource.id,
                action=action,
                encrypted_url_snapshot=resource.encrypted_url,
                encrypted_password_snapshot=resource.encrypted_password,
                state=TaskState.QUEUED,
                attempts=0,
            )
            session.add(task)
            if workflow_id is not None:
                stage_workflow = await link_child(
                    session,
                    workflow_id,
                    WorkflowStageName.PUSH,
                    "task",
                    task.id,
                )
            await session.commit()
            if stage_workflow is not None:
                await emit_workflow_stage_changed(
                    self._event_logger,
                    workflow_id=stage_workflow.id,
                    correlation_id=stage_workflow.correlation_id,
                    stage_name=WorkflowStageName.PUSH,
                    status=WorkflowStageStatus.RUNNING,
                )
            await emit_event(
                self._event_logger,
                "task.submitted",
                fields={"status": "queued", "count": 1},
                task_id=task.id,
                resource_type="task",
                resource_id=task.resource_id,
            )
            return task, False

    async def get(self, task_id: str) -> Task | None:
        async with self._session_factory() as session:
            return await session.get(Task, task_id)

    async def list_recent(self, limit: int = 50, offset: int = 0) -> list[Task]:
        # MCP asks for one look-ahead row to produce a stable next cursor.
        limit = max(1, min(limit, 101))
        offset = max(0, offset)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Task)
                .order_by(Task.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(rows)

    async def retry(
        self,
        task_id: str,
        *,
        allowed_actions: frozenset[TaskAction] | None = None,
    ) -> Task:
        async with self._session_factory() as session:
            stage_workflow = None
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            if allowed_actions is not None and task.action not in allowed_actions:
                raise PushKindUnsupported("push kind is not supported")
            prepare_manual_retry(task)
            if task.workflow_id is not None:
                stage_workflow = await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=WorkflowStageStatus.RUNNING,
                    reason="task_retry",
                )
            await session.commit()
        if stage_workflow is not None:
            await emit_workflow_stage_changed(
                self._event_logger,
                workflow_id=stage_workflow.id,
                correlation_id=stage_workflow.correlation_id,
                stage_name=WorkflowStageName.PUSH,
                status=WorkflowStageStatus.RUNNING,
            )
        return task

    async def cancel(
        self,
        task_id: str,
        *,
        allowed_actions: frozenset[TaskAction] | None = None,
    ) -> Task:
        now = datetime.now(UTC)
        stage_workflow = None
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            if allowed_actions is not None and task.action not in allowed_actions:
                raise PushKindUnsupported("push kind is not supported")
            result = await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.state == TaskState.QUEUED,
                    Task.lease_owner.is_(None),
                )
                .values(
                    state=TaskState.CANCELLED,
                    error_code="cancelled",
                    error_message="task_cancelled",
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise InvalidCancelState("task is not cancellable")
            task.state = TaskState.CANCELLED
            task.error_code = "cancelled"
            task.error_message = "task_cancelled"
            task.updated_at = now
            if task.workflow_id is not None:
                stage_workflow = await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=WorkflowStageStatus.CANCELLED,
                    reason="task_cancelled",
                )
            await session.commit()
        if stage_workflow is not None:
            await emit_workflow_stage_changed(
                self._event_logger,
                workflow_id=stage_workflow.id,
                correlation_id=stage_workflow.correlation_id,
                stage_name=WorkflowStageName.PUSH,
                status=WorkflowStageStatus.CANCELLED,
                error_code="cancelled",
            )
        await emit_event(
            self._event_logger,
            "task.cancelled",
            fields={"status": TaskState.CANCELLED.value},
            task_id=task.id,
            resource_type="task",
            resource_id=task.resource_id,
        )
        return task


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
