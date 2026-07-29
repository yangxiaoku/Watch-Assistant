"""Single-process SQLite-leased task worker."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import Task, TaskState
from watch_assistant.schemas import (
    LoggingLevel,
    RemoteStatus,
    SubmissionResult,
    TaskAction,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.inventory_push_guard import InventoryPushGuard
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.tasks import recover_after_restart
from watch_assistant.services.workflows import sync_child_stage


class TaskAdapter(Protocol):
    async def submit_magnet(self, url: str) -> SubmissionResult: ...

    async def save_share(self, url: str, password: str | None) -> SubmissionResult: ...

    async def get_status(self, remote_ref: str) -> RemoteStatus | None: ...


class TaskWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        adapter: TaskAdapter,
        *,
        owner: str,
        lease_seconds: int = 60,
        event_logger: EventLogger | None = None,
        inventory_guard: InventoryPushGuard | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._adapter = adapter
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._event_logger = event_logger
        self._inventory_guard = inventory_guard

    async def run_once(self) -> bool:
        task_id = await self._claim_one()
        if task_id is None:
            return False
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return False
            if task.action != TaskAction.OFFLINE_DOWNLOAD:
                result = SubmissionResult(
                    status=RemoteStatus.FAILED,
                    error_code="push_kind_unsupported",
                    error_message="share push is not supported",
                )
            else:
                gate = None
                if self._inventory_guard is not None:
                    try:
                        gate = await self._inventory_guard.check(task.resource_id)
                    except Exception:  # noqa: BLE001 - fail closed before remote submission
                        gate = None
                if self._inventory_guard is not None and (
                    gate is None or not gate.allowed
                ):
                    result = SubmissionResult(
                        status=RemoteStatus.FAILED,
                        error_code=(
                            "inventory_check_failed" if gate is None else gate.code
                        ),
                        error_message="inventory preflight blocked remote submission",
                    )
                else:
                    try:
                        url = self._crypto.decrypt(task.encrypted_url_snapshot)
                    except Exception:  # noqa: BLE001 - failure occurred before remote submission
                        result = SubmissionResult(
                            status=RemoteStatus.FAILED,
                            error_code="local_decryption_failed",
                            error_message="stored submission data could not be decrypted",
                        )
                    else:
                        try:
                            result = await self._adapter.submit_magnet(url)
                        except Exception:  # noqa: BLE001 - remote outcome may be ambiguous
                            result = SubmissionResult(
                                status=RemoteStatus.UNCERTAIN,
                                error_code="adapter_error",
                                error_message="submission outcome is uncertain",
                            )

            task.state = TaskState(result.status.value)
            task.remote_ref = result.remote_ref
            task.error_code = result.error_code
            task.error_message = result.error_message
            task.submitted_at = (
                datetime.now(UTC) if result.status == RemoteStatus.ACCEPTED else None
            )
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = datetime.now(UTC)
            if task.workflow_id is not None:
                await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=_workflow_stage_status(result.status),
                    reason=f"task_{result.status.value}",
                    error_code=result.error_code,
                )
            await session.commit()
        await emit_event(
            self._event_logger,
            "task.accepted"
            if result.status == RemoteStatus.ACCEPTED
            else "task.failed",
            level=(
                LoggingLevel.INFO
                if result.status == RemoteStatus.ACCEPTED
                else LoggingLevel.WARNING
            ),
            fields={"status": result.status.value, "count": 1},
        )
        return True

    async def recover_expired(self) -> int:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            tasks = list(
                await session.scalars(
                    select(Task).where(
                        Task.state == TaskState.SUBMITTING,
                        Task.lease_expires_at.is_not(None),
                        Task.lease_expires_at < now,
                    )
                )
            )
            for task in tasks:
                if task.action != TaskAction.OFFLINE_DOWNLOAD:
                    task.state = TaskState.FAILED
                    task.remote_ref = None
                    task.error_code = "push_kind_unsupported"
                    task.error_message = "share push is not supported"
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.updated_at = now
                    if task.workflow_id is not None:
                        await sync_child_stage(
                            session,
                            task.workflow_id,
                            WorkflowStageName.PUSH,
                            child_type="task",
                            child_id=task.id,
                            status=WorkflowStageStatus.FAILED,
                            reason="task_failed",
                            error_code=task.error_code,
                        )
                    continue
                remote_status = None
                if task.remote_ref:
                    try:
                        remote_status = await self._adapter.get_status(task.remote_ref)
                    except Exception:  # noqa: BLE001 - status failure is uncertain
                        remote_status = None
                recover_after_restart(task, remote_status)
                if task.workflow_id is not None:
                    await sync_child_stage(
                        session,
                        task.workflow_id,
                        WorkflowStageName.PUSH,
                        child_type="task",
                        child_id=task.id,
                        status=_workflow_stage_status(task.state),
                        reason=f"task_{task.state.value}",
                        error_code=task.error_code,
                    )
            await session.commit()
            return len(tasks)

    async def run_forever(self, stop_event: asyncio.Event, *, interval: float = 1.0):
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _claim_one(self) -> str | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.state == TaskState.QUEUED,
                    (Task.lease_expires_at.is_(None) | (Task.lease_expires_at < now)),
                )
                .order_by(Task.created_at)
                .limit(1)
            )
            if task is None:
                return None
            task.state = TaskState.SUBMITTING
            task.attempts += 1
            task.lease_owner = self._owner
            task.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            task.updated_at = now
            await session.commit()
            return task.id


def _workflow_stage_status(
    status: RemoteStatus | TaskState,
) -> WorkflowStageStatus:
    value = status.value
    if value == RemoteStatus.ACCEPTED.value:
        return WorkflowStageStatus.SUCCEEDED
    if value == RemoteStatus.NEEDS_AUTH.value:
        return WorkflowStageStatus.WAITING_CONFIRMATION
    if value == RemoteStatus.UNCERTAIN.value:
        return WorkflowStageStatus.UNCERTAIN
    if value == RemoteStatus.FAILED.value:
        return WorkflowStageStatus.FAILED
    return WorkflowStageStatus.RUNNING
