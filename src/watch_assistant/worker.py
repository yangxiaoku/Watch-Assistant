"""Single-process SQLite-leased task worker."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import Protocol, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import TaskState
from watch_assistant.schemas import (
    LoggingLevel,
    RemoteObservation,
    RemoteStatus,
    SubmissionResult,
    TaskAction,
)
from watch_assistant.services.inventory_push_guard import (
    InventoryPushCheck,
    InventoryPushGuard,
    InventoryRefreshEvidence,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.tasks import (
    RECONCILIATION_UNAVAILABLE,
    TASK_LEASE_LOST,
    TaskLease,
    TaskService,
    read_task_status,
)

_ExternalResult = TypeVar("_ExternalResult")


class _LeaseClaimLost(RuntimeError):
    """The worker must discard an external result after losing its fence."""


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

    async def get_status_for_task(
        self,
        remote_ref: str,
        *,
        target_directory_id: str | None,
    ) -> RemoteStatus | RemoteObservation | None: ...


class TaskWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        adapter: TaskAdapter,
        *,
        owner: str,
        lease_seconds: float = 60,
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
        self._lease_seconds = float(lease_seconds)
        if self._lease_seconds <= 0:
            raise ValueError("invalid_lease_duration")
        self._event_logger = event_logger
        self._inventory_guard = inventory_guard
        self._inventory_refresh = inventory_refresh
        self._tasks = TaskService(session_factory)

    async def run_once(self) -> bool:
        lease = await self._claim_one()
        if lease is None:
            return False
        try:
            result = await self._process_lease(lease)
        except _LeaseClaimLost:
            await self._mark_lease_lost(lease)
            return True
        except asyncio.CancelledError:
            await self._mark_lease_lost(lease)
            raise
        except Exception:  # noqa: BLE001 - keep a failed claim recoverable
            await self._mark_lease_lost(lease)
            return True
        try:
            task = await self._tasks.finish_submission(lease, result)
        except asyncio.CancelledError:
            await self._mark_lease_lost(lease)
            raise
        except Exception:  # noqa: BLE001 - keep a failed claim recoverable
            await self._mark_lease_lost(lease)
            return True
        if task is None:
            return True
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
        recovered = 0
        lease_duration = timedelta(seconds=self._lease_seconds)
        while True:
            lease = await self._tasks.claim_expired(
                owner=self._owner, lease_duration=lease_duration
            )
            if lease is None:
                return recovered
            recovered += 1
            if lease.action != TaskAction.OFFLINE_DOWNLOAD:
                try:
                    await self._tasks.finish_submission(
                        lease,
                        SubmissionResult(
                            status=RemoteStatus.FAILED,
                            error_code="push_kind_unsupported",
                            error_message="share push is not supported",
                        ),
                    )
                except asyncio.CancelledError:
                    await self._mark_lease_lost(lease)
                    raise
                except Exception:  # noqa: BLE001 - recovery retries after expiry
                    await self._mark_lease_lost(lease)
                continue

            remote_status = None
            if lease.remote_ref:
                remote_ref = lease.remote_ref
                target_directory_id = lease.target_directory_id
                try:
                    read_status = partial(
                        read_task_status,
                        self._adapter,
                        remote_ref,
                        target_directory_id=target_directory_id,
                    )
                    remote_status = await self._run_external_call(
                        lease, read_status
                    )
                except _LeaseClaimLost:
                    await self._mark_lease_lost(lease)
                    continue
                except asyncio.CancelledError:
                    await self._mark_lease_lost(lease)
                    raise
                except Exception:  # noqa: BLE001 - status failure is uncertain
                    remote_status = RemoteObservation(
                        status=RemoteStatus.UNCERTAIN,
                        error_code=RECONCILIATION_UNAVAILABLE,
                    )
            try:
                await self._tasks.finish_recovery(lease, remote_status)
            except asyncio.CancelledError:
                await self._mark_lease_lost(lease)
                raise
            except Exception:  # noqa: BLE001 - recovery retries after expiry
                await self._mark_lease_lost(lease)

    async def run_forever(self, stop_event: asyncio.Event, *, interval: float = 1.0):
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A transient claim/database error must not kill the worker loop.
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _claim_one(self) -> TaskLease | None:
        return await self._tasks.claim_next(
            owner=self._owner,
            lease_duration=timedelta(seconds=self._lease_seconds),
        )

    async def _process_lease(self, lease: TaskLease) -> SubmissionResult:
        if lease.action != TaskAction.OFFLINE_DOWNLOAD:
            return SubmissionResult(
                status=RemoteStatus.FAILED,
                error_code="push_kind_unsupported",
                error_message="share push is not supported",
            )

        gate = None
        if self._inventory_guard is not None:
            try:
                gate = await self._run_external_call(
                    lease, lambda: self._inventory_guard.check(lease.resource_id)
                )
            except _LeaseClaimLost:
                raise
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
                refreshed = await self._run_external_call(
                    lease, self._inventory_refresh
                )
            except _LeaseClaimLost:
                raise
            except Exception:  # noqa: BLE001 - fail closed before remote submission
                gate = InventoryPushCheck(False, "inventory_check_failed")
                refreshed = False
            if isinstance(refreshed, InventoryRefreshEvidence):
                if refreshed.usable:
                    try:
                        gate = await self._run_external_call(
                            lease,
                            lambda: self._inventory_guard.check(lease.resource_id),
                        )
                    except _LeaseClaimLost:
                        raise
                    except Exception:  # noqa: BLE001 - fail closed before remote submission
                        gate = None
                else:
                    gate = InventoryPushCheck(
                        False,
                        refreshed.error_code or "inventory_check_failed",
                    )
            elif refreshed:
                try:
                    gate = await self._run_external_call(
                        lease, lambda: self._inventory_guard.check(lease.resource_id)
                    )
                except _LeaseClaimLost:
                    raise
                except Exception:  # noqa: BLE001 - fail closed before remote submission
                    gate = None
        if self._inventory_guard is not None and (gate is None or not gate.allowed):
            code = "inventory_check_failed" if gate is None else gate.code
            return SubmissionResult(
                status=RemoteStatus.FAILED,
                error_code=code,
                error_message=_inventory_error_message(code),
            )

        try:
            url = self._crypto.decrypt(lease.encrypted_url_snapshot)
        except Exception:  # noqa: BLE001 - failure occurred before remote submission
            return SubmissionResult(
                status=RemoteStatus.FAILED,
                error_code="local_decryption_failed",
                error_message="stored submission data could not be decrypted",
            )
        try:
            if lease.target_directory_id is None:
                return await self._run_external_call(
                    lease, lambda: self._adapter.submit_magnet(url)
                )
            return await self._run_external_call(
                lease,
                lambda: self._adapter.submit_magnet(
                    url, target_cid=lease.target_directory_id
                ),
            )
        except _LeaseClaimLost:
            raise
        except Exception:  # noqa: BLE001 - remote outcome may be ambiguous
            return SubmissionResult(
                status=RemoteStatus.UNCERTAIN,
                error_code="adapter_error",
                error_message="submission outcome is uncertain",
            )

    async def _run_external_call(
        self,
        lease: TaskLease,
        operation: Callable[[], Awaitable[_ExternalResult]],
    ) -> _ExternalResult:
        if not await self._lease_is_active(lease):
            raise _LeaseClaimLost
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._renew_lease_forever(lease, heartbeat_stop, lease_lost),
            name=f"watch-assistant-task-lease-{lease.task_id}",
        )
        operation_task = None

        async def invoke_operation() -> _ExternalResult:
            # Recheck the fencing identity in the same task immediately before
            # entering the adapter coroutine.  This closes the scheduling gap
            # between the initial check and creation of the external call.
            if not await self._lease_is_active(lease):
                raise _LeaseClaimLost
            return await operation()

        try:
            operation_task = asyncio.ensure_future(invoke_operation())
            done, _pending = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and lease_lost.is_set():
                if not operation_task.done():
                    operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task
                raise _LeaseClaimLost
            try:
                result = await operation_task
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    raise _LeaseClaimLost
                raise
            except Exception as exc:
                if lease_lost.is_set() or not await self._lease_is_active(lease):
                    raise _LeaseClaimLost from exc
                raise
            if lease_lost.is_set() or not await self._lease_is_active(lease):
                raise _LeaseClaimLost
        finally:
            await self._stop_lease_heartbeat(heartbeat_stop, heartbeat_task)
            if operation_task is not None and not operation_task.done():
                operation_task.cancel()
            if operation_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await operation_task
        if lease_lost.is_set():
            raise _LeaseClaimLost
        return result

    async def _stop_lease_heartbeat(
        self, stop_event: asyncio.Event, heartbeat_task: asyncio.Task[None]
    ) -> None:
        """Stop renewal without interrupting an in-flight database commit."""

        stop_event.set()
        if heartbeat_task.done():
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            return

        grace_period = max(min(self._lease_seconds / 3, 5.0), 0.05) * 2
        try:
            await asyncio.wait_for(
                asyncio.shield(heartbeat_task), timeout=grace_period
            )
        except TimeoutError:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        except asyncio.CancelledError:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            raise

    async def _lease_is_active(self, lease: TaskLease) -> bool:
        try:
            return await self._tasks.is_lease_active(lease)
        except Exception:  # noqa: BLE001 - fail closed when the DB cannot fence
            return False

    async def _mark_lease_lost(self, lease: TaskLease) -> None:
        """Leave a still-owned claim reviewable without crossing its fence."""

        try:
            task = await self._tasks.mark_lease_lost(lease)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - recovery retries after expiry
            return
        if task is None:
            return
        await emit_event(
            self._event_logger,
            "task.uncertain",
            level=LoggingLevel.WARNING,
            fields={
                "status": task.state.value,
                "count": 1,
                "error_code": TASK_LEASE_LOST,
            },
            task_id=task.id,
            resource_type="task",
            resource_id=task.resource_id,
        )

    async def _renew_lease_forever(
        self,
        lease: TaskLease,
        stop_event: asyncio.Event,
        failure_event: asyncio.Event,
    ) -> None:
        interval = max(min(self._lease_seconds / 3, 30.0), 0.01)
        # A SQLite commit can outlive one heartbeat interval under runner load.
        # Keep the timeout inside the lease budget; the renewal predicate still
        # rejects a claim that expires while the database call is in flight.
        renew_timeout = max(min(self._lease_seconds / 2, 5.0), 0.05)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                try:
                    renewed = await asyncio.wait_for(
                        self._tasks.renew(
                            lease,
                            lease_duration=timedelta(seconds=self._lease_seconds),
                        ),
                        timeout=renew_timeout,
                    )
                except Exception:  # noqa: BLE001 - external result must be discarded
                    failure_event.set()
                    return
                if not renewed:
                    failure_event.set()
                    return
def _inventory_error_message(code: str) -> str:
    return _INVENTORY_ERROR_MESSAGES_ZH.get(
        code, "库存检查未完成，已阻止远端提交。"
    )
