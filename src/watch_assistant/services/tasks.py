"""Task creation, idempotency, and state transitions."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Resource, Task, TaskState, WorkflowEvidence
from watch_assistant.schemas import (
    EvidenceSource,
    EvidenceStatus,
    RemoteObservation,
    RemoteStatus,
    TaskAction,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.workflows import (
    WorkflowConflict,
    advance_availability_from_evidence,
    link_child,
    record_evidence,
    sync_child_stage,
)

REUSABLE_STATES = (
    TaskState.QUEUED,
    TaskState.SUBMITTING,
    TaskState.SUBMITTED,
    TaskState.DOWNLOADING,
    TaskState.AVAILABLE,
)


class TaskStatusAdapter(Protocol):
    async def get_status(
        self, remote_ref: str
    ) -> RemoteStatus | RemoteObservation | None: ...


class ResourceNotFound(LookupError):
    pass


class InvalidRetryState(ValueError):
    pass


class InvalidCancelState(ValueError):
    pass


class PushKindUnsupported(ValueError):
    pass


class TaskNotReconcilable(ValueError):
    pass


class ReconciliationUnavailable(RuntimeError):
    pass


AVAILABILITY_OBSERVATION_UNVERIFIED = "availability_observation_unverified"


RemoteState = RemoteStatus | RemoteObservation


def task_state_from_remote_status(
    status: RemoteStatus, *, allow_available: bool = False
) -> TaskState:
    del allow_available
    if status in {RemoteStatus.ACCEPTED, RemoteStatus.SUBMITTED}:
        return TaskState.SUBMITTED
    if status is RemoteStatus.DOWNLOADING:
        return TaskState.DOWNLOADING
    if status is RemoteStatus.NEEDS_AUTH:
        return TaskState.NEEDS_AUTH
    if status is RemoteStatus.FAILED:
        return TaskState.FAILED
    return TaskState.UNCERTAIN


def task_state_from_remote_observation(observation: RemoteObservation) -> TaskState:
    """Convert a remote result without trusting a bare AVAILABLE marker."""

    try:
        status = RemoteStatus(observation.status)
    except (TypeError, ValueError):
        return TaskState.UNCERTAIN
    if status is RemoteStatus.AVAILABLE:
        return (
            TaskState.AVAILABLE
            if observation.availability_verified
            else TaskState.UNCERTAIN
        )
    return task_state_from_remote_status(status)


def workflow_stage_status_for_task_state(state: TaskState) -> WorkflowStageStatus:
    if state in {TaskState.SUBMITTED, TaskState.DOWNLOADING}:
        return WorkflowStageStatus.WAITING_EXTERNAL
    if state is TaskState.NEEDS_AUTH:
        return WorkflowStageStatus.WAITING_CONFIRMATION
    if state is TaskState.UNCERTAIN:
        return WorkflowStageStatus.UNCERTAIN
    if state is TaskState.FAILED:
        return WorkflowStageStatus.FAILED
    if state is TaskState.CANCELLED:
        return WorkflowStageStatus.CANCELLED
    return WorkflowStageStatus.RUNNING


def evidence_status_for_task_state(state: TaskState) -> EvidenceStatus:
    if state is TaskState.SUBMITTED:
        return EvidenceStatus.SUBMITTED
    if state is TaskState.DOWNLOADING:
        return EvidenceStatus.DOWNLOADING
    if state is TaskState.AVAILABLE:
        return EvidenceStatus.AVAILABLE
    if state is TaskState.FAILED:
        return EvidenceStatus.FAILED
    return EvidenceStatus.UNCERTAIN


async def apply_remote_status(
    session: AsyncSession,
    task: Task,
    remote_status: RemoteState,
    *,
    source: EvidenceSource,
    verified_available: bool = False,
) -> WorkflowEvidence:
    """Apply a remote result without trusting a caller-supplied AVAILABLE flag."""

    del verified_available
    observation = _as_remote_observation(remote_status)
    if observation is None:
        raise WorkflowConflict("workflow_evidence_required")
    state = task_state_from_remote_observation(observation)
    availability_verified = state is TaskState.AVAILABLE
    if availability_verified and source is not EvidenceSource.READONLY_RECONCILIATION:
        raise WorkflowConflict("workflow_evidence_required")
    if task.state is TaskState.AVAILABLE and (
        not availability_verified
    ):
        raise WorkflowConflict("workflow_stage_terminal")
    task.state = state
    if state not in {TaskState.FAILED, TaskState.UNCERTAIN}:
        task.error_code = None
        task.error_message = None
    elif observation.error_code is not None:
        task.error_code = observation.error_code
        task.error_message = _observation_error_message(observation.error_code)
    task.updated_at = datetime.now(UTC)
    evidence = await record_evidence(
        session,
        workflow_id=task.workflow_id,
        task_id=task.id,
        stage=(
            WorkflowStageName.AVAILABILITY
            if state is TaskState.AVAILABLE
            else WorkflowStageName.PUSH
        ),
        evidence_type=(
            "availability_receipt" if state is TaskState.AVAILABLE else "remote_status"
        ),
        source=source,
        subject_id=task.id,
        status=evidence_status_for_task_state(state),
        verified=availability_verified,
    )
    if task.workflow_id is not None:
        if availability_verified:
            await advance_availability_from_evidence(
                session, task.workflow_id, task.id, evidence
            )
        else:
            await sync_child_stage(
                session,
                task.workflow_id,
                WorkflowStageName.PUSH,
                child_type="task",
                child_id=task.id,
                status=workflow_stage_status_for_task_state(state),
                reason=f"task_{state.value}",
                error_code=task.error_code,
            )
    return evidence


def recover_after_restart(task: Task, remote_status: RemoteState | None) -> None:
    task.lease_owner = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)
    if task.state is TaskState.AVAILABLE:
        return
    if remote_status is None:
        task.state = TaskState.UNCERTAIN
        return
    observation = _as_remote_observation(remote_status)
    if observation is None:
        task.state = TaskState.UNCERTAIN
        return
    task.state = task_state_from_remote_observation(observation)
    if observation.error_code is not None:
        task.error_code = observation.error_code
        task.error_message = _observation_error_message(observation.error_code)


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
    if task.state is TaskState.UNCERTAIN:
        raise InvalidRetryState("uncertain_requires_verification")
    if task.state not in {
        TaskState.FAILED,
        TaskState.NEEDS_AUTH,
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
        self._reconcile_lock = asyncio.Lock()
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
                        await link_child(
                            session,
                            workflow_id,
                            WorkflowStageName.PUSH,
                            "task",
                            existing.id,
                        )
                        if existing.state is TaskState.AVAILABLE:
                            evidence = await session.scalar(
                                select(WorkflowEvidence)
                                .where(
                                    WorkflowEvidence.task_id == existing.id,
                                    WorkflowEvidence.stage
                                    == WorkflowStageName.AVAILABILITY,
                                    WorkflowEvidence.status
                                    == EvidenceStatus.AVAILABLE.value,
                                    WorkflowEvidence.verified.is_(True),
                                )
                                .order_by(WorkflowEvidence.observed_at.desc())
                            )
                            if evidence is not None:
                                await advance_availability_from_evidence(
                                    session, workflow_id, existing.id, evidence
                                )
                        await session.commit()
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
                await link_child(
                    session,
                    workflow_id,
                    WorkflowStageName.PUSH,
                    "task",
                    task.id,
                )
            await session.commit()
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

    async def reconcile(
        self,
        task_id: str,
        adapter: TaskStatusAdapter,
    ) -> tuple[Task, WorkflowEvidence]:
        """Read the remote state and persist the observation; never submit."""

        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            if not task.remote_ref:
                raise TaskNotReconcilable("task_not_reconcilable")
            remote_ref = task.remote_ref
            target_directory_id = task.target_directory_id
        try:
            remote_status = await _read_task_status(
                adapter, remote_ref, target_directory_id=target_directory_id
            )
        except Exception as exc:
            raise ReconciliationUnavailable("reconciliation_unavailable") from exc
        if remote_status is None:
            raise ReconciliationUnavailable("reconciliation_unavailable")
        normalized_observation = _as_remote_observation(remote_status)
        if normalized_observation is None:
            raise ReconciliationUnavailable("reconciliation_unavailable")

        async with self._reconcile_lock, self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            if task.remote_ref != remote_ref:
                raise ReconciliationUnavailable("reconciliation_conflict")
            evidence = await apply_remote_status(
                session,
                task,
                normalized_observation,
                source=EvidenceSource.READONLY_RECONCILIATION,
            )
            await session.commit()
            return task, evidence

    async def evidence(self, task_id: str) -> list[WorkflowEvidence]:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            return list(
                await session.scalars(
                    select(WorkflowEvidence)
                    .where(WorkflowEvidence.task_id == task_id)
                    .order_by(WorkflowEvidence.observed_at, WorkflowEvidence.id)
                )
            )

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
            task = await session.get(Task, task_id)
            if task is None:
                raise ResourceNotFound(task_id)
            if allowed_actions is not None and task.action not in allowed_actions:
                raise PushKindUnsupported("push kind is not supported")
            prepare_manual_retry(task)
            if task.workflow_id is not None:
                await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=WorkflowStageStatus.RUNNING,
                    reason="task_retry",
                )
            await session.commit()
            return task

    async def cancel(
        self,
        task_id: str,
        *,
        allowed_actions: frozenset[TaskAction] | None = None,
    ) -> Task:
        now = datetime.now(UTC)
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
                await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=WorkflowStageStatus.CANCELLED,
                    reason="task_cancelled",
                )
            await session.commit()
        await emit_event(
            self._event_logger,
            "task.cancelled",
            fields={"status": TaskState.CANCELLED.value},
            task_id=task.id,
            resource_type="task",
            resource_id=task.resource_id,
        )
        return task


async def _read_task_status(
    adapter: TaskStatusAdapter,
    remote_ref: str,
    *,
    target_directory_id: str | None,
) -> RemoteState | None:
    """Use a target-aware adapter hook when it exists, preserving old adapters."""

    target_aware = getattr(adapter, "get_status_for_task", None)
    if callable(target_aware):
        return await target_aware(
            remote_ref, target_directory_id=target_directory_id
        )
    return await adapter.get_status(remote_ref)


def _as_remote_observation(value: object) -> RemoteObservation | None:
    if isinstance(value, RemoteObservation):
        try:
            status = RemoteStatus(value.status)
        except (TypeError, ValueError):
            return None
        error_code = value.error_code
        if status is RemoteStatus.AVAILABLE and not value.availability_verified:
            error_code = error_code or AVAILABILITY_OBSERVATION_UNVERIFIED
        if status is value.status and error_code == value.error_code:
            return value
        return RemoteObservation(
            status=status,
            file_id=value.file_id,
            parent_id=value.parent_id,
            is_directory=value.is_directory,
            error_code=error_code,
        )
    try:
        status = RemoteStatus(value)
    except (TypeError, ValueError):
        return None
    return RemoteObservation(
        status=status,
        error_code=(
            AVAILABILITY_OBSERVATION_UNVERIFIED
            if status is RemoteStatus.AVAILABLE
            else None
        ),
    )


def _observation_error_message(error_code: str) -> str:
    return {
        AVAILABILITY_OBSERVATION_UNVERIFIED: "远端文件可用性尚未完成只读核验。",
        "availability_file_id_unavailable": "无法可靠取得远端文件 ID。",
        "availability_parent_mismatch": "远端文件父目录核验不一致。",
        "availability_observer_unavailable": "远端文件只读观察器暂不可用。",
    }.get(error_code, "远端结果待确认，系统未重复提交。")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
