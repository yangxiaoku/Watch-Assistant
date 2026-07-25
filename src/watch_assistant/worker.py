"""Single-process SQLite-leased task worker."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import Task, TaskState
from watch_assistant.schemas import RemoteStatus, SubmissionResult, TaskAction
from watch_assistant.services.tasks import recover_after_restart


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
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._adapter = adapter
        self._owner = owner
        self._lease_seconds = lease_seconds

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
            await session.commit()
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
                    continue
                remote_status = None
                if task.remote_ref:
                    try:
                        remote_status = await self._adapter.get_status(task.remote_ref)
                    except Exception:  # noqa: BLE001 - status failure is uncertain
                        remote_status = None
                recover_after_restart(task, remote_status)
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
