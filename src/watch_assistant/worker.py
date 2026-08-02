"""Single-process SQLite-leased task worker."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import Task, TaskState
from watch_assistant.schemas import (
    EvidenceSource,
    LoggingLevel,
    RemoteObservation,
    RemoteStatus,
    SubmissionResult,
    TaskAction,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.inventory_push_guard import (
    InventoryPushCheck,
    InventoryPushGuard,
    InventoryRefreshEvidence,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.tasks import (
    apply_remote_status,
    recover_after_restart,
    workflow_stage_status_for_task_state,
)
from watch_assistant.services.workflows import sync_child_stage

_AUTO_REFRESH_INVENTORY_CODES = frozenset(
    {
        "inventory_index_stale",
        "inventory_index_incomplete",
        "inventory_index_unknown",
    }
)
_INVENTORY_ERROR_MESSAGES_ZH = {
    "inventory_scope_unconfigured": "媒体库库存范围未配置，已阻止远端提交。",
    "inventory_index_incomplete": "媒体库库存扫描不完整，已阻止远端提交。",
    "inventory_index_stale": "媒体库库存索引已过期，已阻止远端提交。",
    "inventory_index_unknown": "媒体库库存状态未知，已阻止远端提交。",
    "inventory_exact_duplicate": "媒体库已有相同资源，已阻止远端提交。",
    "inventory_check_failed": "库存检查未完成，已阻止远端提交。",
}


class TaskAdapter(Protocol):
    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult: ...

    async def save_share(
        self,
        url: str,
        password: str | None,
        *,
        target_cid: str | None = None,
    ) -> SubmissionResult: ...

    async def get_status(
        self, remote_ref: str
    ) -> RemoteStatus | RemoteObservation | None: ...


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
        inventory_refresh: (
            Callable[[], Awaitable[bool | InventoryRefreshEvidence]] | None
        ) = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._adapter = adapter
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._event_logger = event_logger
        self._inventory_guard = inventory_guard
        self._inventory_refresh = inventory_refresh

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
                if (
                    self._inventory_guard is not None
                    and gate is not None
                    and not gate.allowed
                    and gate.code in _AUTO_REFRESH_INVENTORY_CODES
                    and self._inventory_refresh is not None
                ):
                    try:
                        refreshed = await self._inventory_refresh()
                    except Exception:  # noqa: BLE001 - fail closed before remote submission
                        gate = InventoryPushCheck(False, "inventory_check_failed")
                        refreshed = False
                    if isinstance(refreshed, InventoryRefreshEvidence):
                        if refreshed.usable:
                            try:
                                gate = await self._inventory_guard.check(task.resource_id)
                            except Exception:  # noqa: BLE001 - fail closed before remote submission
                                gate = None
                        else:
                            gate = InventoryPushCheck(
                                False,
                                refreshed.error_code or "inventory_check_failed",
                            )
                    elif refreshed:
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
                        error_message=_inventory_error_message(
                            "inventory_check_failed" if gate is None else gate.code
                        ),
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
                            if task.target_directory_id is None:
                                result = await self._adapter.submit_magnet(url)
                            else:
                                result = await self._adapter.submit_magnet(
                                    url, target_cid=task.target_directory_id
                                )
                        except Exception:  # noqa: BLE001 - remote outcome may be ambiguous
                            result = SubmissionResult(
                                status=RemoteStatus.UNCERTAIN,
                                error_code="adapter_error",
                                error_message="submission outcome is uncertain",
                            )

            task.remote_ref = result.remote_ref
            task.error_code = result.error_code
            task.error_message = result.error_message
            task.submitted_at = (
                datetime.now(UTC)
                if result.status
                in {
                    RemoteStatus.ACCEPTED,
                    RemoteStatus.SUBMITTED,
                    RemoteStatus.DOWNLOADING,
                    RemoteStatus.AVAILABLE,
                }
                else None
            )
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = datetime.now(UTC)
            await apply_remote_status(
                session,
                task,
                result.status,
                source=EvidenceSource.SUBMISSION_RECEIPT,
                verified_available=False,
            )
            await session.commit()
        state = task.state
        event_code = {
            TaskState.SUBMITTED: "task.submitted",
            TaskState.DOWNLOADING: "task.downloading",
            TaskState.AVAILABLE: "task.availability_verified",
            TaskState.UNCERTAIN: "task.uncertain",
            TaskState.FAILED: "task.failed",
            TaskState.NEEDS_AUTH: "task.failed",
        }.get(state, "task.failed")
        await emit_event(
            self._event_logger,
            event_code,
            level=(
                LoggingLevel.INFO
                if state
                in {
                    TaskState.SUBMITTED,
                    TaskState.DOWNLOADING,
                    TaskState.AVAILABLE,
                }
                else LoggingLevel.WARNING
            ),
            fields={
                "status": state.value,
                "count": 1,
                "error_code": result.error_code,
            },
            task_id=task.id,
            resource_type="task",
            resource_id=task.resource_id,
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
                        remote_status = await _read_task_status(
                            self._adapter,
                            task.remote_ref,
                            target_directory_id=task.target_directory_id,
                        )
                    except Exception:  # noqa: BLE001 - status failure is uncertain
                        remote_status = None
                recover_after_restart(task, remote_status)
                if remote_status is not None:
                    await apply_remote_status(
                        session,
                        task,
                        remote_status,
                        source=EvidenceSource.READONLY_RECONCILIATION,
                    )
                elif task.workflow_id is not None:
                    await sync_child_stage(
                        session,
                        task.workflow_id,
                        WorkflowStageName.PUSH,
                        child_type="task",
                        child_id=task.id,
                        status=workflow_stage_status_for_task_state(task.state),
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
def _inventory_error_message(code: str) -> str:
    return _INVENTORY_ERROR_MESSAGES_ZH.get(
        code, "库存检查未完成，已阻止远端提交。"
    )


async def _read_task_status(
    adapter: TaskAdapter,
    remote_ref: str,
    *,
    target_directory_id: str | None,
) -> RemoteStatus | RemoteObservation | None:
    target_aware = getattr(adapter, "get_status_for_task", None)
    if callable(target_aware):
        return await target_aware(
            remote_ref, target_directory_id=target_directory_id
        )
    return await adapter.get_status(remote_ref)
