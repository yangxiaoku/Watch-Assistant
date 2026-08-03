"""Durable consumer for organization completion events and STRM updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.library_models import (
    LibraryScanEntry,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    OrganizationOperation,
)
from watch_assistant.schemas import WorkflowStageName, WorkflowStageStatus
from watch_assistant.services.empty_directory_cleanup import EmptyDirectoryCleanupStatus
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
)
from watch_assistant.services.observability import emit_event
from watch_assistant.services.organization_outbox import (
    DIRTY_RUNNING,
    GENERATION_RUNNING,
    DirectoryDirtyLease,
    DirectoryDirtyOutboxService,
)
from watch_assistant.services.strm_manifest import (
    StrmGenerationSummary,
    StrmManifestError,
    StrmManifestService,
)
from watch_assistant.services.strm_operations import (
    StrmOperationError,
    StrmOperationKind,
    StrmOperationNotFound,
    StrmOperationService,
    StrmOperationSummary,
)
from watch_assistant.services.workflows import (
    WorkflowConflict,
    WorkflowNotFound,
    sync_child_stage,
)

IndexFactory = Callable[[str, str], LibraryIndexService]
EmptyDirectoryCleaner = Callable[[str, str, str], Awaitable[EmptyDirectoryCleanupStatus]]


@dataclass(frozen=True, slots=True)
class _CleanupCandidate:
    directory_id: str
    parent_id: str
    name: str


class DirectoryDirtyWorker:
    """Reconcile one durable dirty event at a time.

    The first implementation uses a complete, verified tree snapshot as the
    reconciliation boundary. This is deliberately conservative: it gives the
    manifest the same deletion safety proof as manual full generation while
    still making organization completion events automatic and retryable.
    """

    def __init__(
        self,
        session_factory,
        strm_service: StrmManifestService,
        index_factory: IndexFactory,
        *,
        output_root: Path | str,
        playback_url_prefix: str,
        cleanup_enabled: bool = False,
        outbox: DirectoryDirtyOutboxService | None = None,
        operation_service: StrmOperationService | None = None,
        settings_service=None,
        empty_directory_cleaner: EmptyDirectoryCleaner | None = None,
        poll_interval_seconds: float = 5.0,
        max_attempts: int = 5,
        event_logger=None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_poll_interval")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise ValueError("invalid_max_attempts")
        self._session_factory = session_factory
        self._strm = strm_service
        self._index_factory = index_factory
        self._output_root = Path(output_root)
        self._playback_url_prefix = playback_url_prefix
        self._cleanup_enabled = bool(cleanup_enabled)
        self._outbox = outbox or DirectoryDirtyOutboxService()
        self._operations = operation_service or StrmOperationService(session_factory)
        self._settings_service = settings_service
        self._empty_directory_cleaner = empty_directory_cleaner
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_attempts = max_attempts
        self._event_logger = event_logger
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        lease = await self._outbox.claim_generation(self._session_factory)
        if lease is None:
            return False
        workflow_id: str | None = None
        operation: StrmOperationSummary | None = None
        operation_lease_owner: str | None = None
        heartbeat_stop: asyncio.Event | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            context = await self._load_context(lease)
            if context is None:
                await self._outbox.complete(
                    self._session_factory,
                    lease,
                    status="failed",
                    error_code="event_scope_unverified",
                )
                return True
            library_id, root_directory_id, actions_json, workflow_id = context
            await self._sync_workflow(
                workflow_id,
                lease,
                status=WorkflowStageStatus.RUNNING,
                reason="strm_started",
            )
            strm_linkage_enabled = True
            cleanup_empty_directories = False
            if self._settings_service is not None:
                try:
                    organization_settings = await self._settings_service.get_organization()
                except Exception:  # noqa: BLE001 - do not lose a dirty event on settings failure
                    await self._outbox.retry(
                        self._session_factory,
                        lease,
                        error_code="settings_unavailable",
                        max_attempts=self._max_attempts,
                    )
                    return True
                strm_linkage_enabled = organization_settings.strm_linkage_enabled
                cleanup_empty_directories = organization_settings.cleanup_empty_directories
                if not strm_linkage_enabled:
                    await self._sync_workflow(
                        workflow_id,
                        lease,
                        status=WorkflowStageStatus.SKIPPED,
                        reason="strm_linkage_disabled",
                    )
                if not strm_linkage_enabled and not cleanup_empty_directories:
                    completed = await self._outbox.complete(
                        self._session_factory,
                        lease,
                        error_code="strm_linkage_disabled",
                    )
                    if not completed:
                        return True
                    await self._audit("strm.dirty_skipped", "整理完成后未启用 STRM 联动")
                    return True
            index = self._index_factory(library_id, root_directory_id)
            scan = await index.scan_tree(_dirty_idempotency_key(lease))
            if not scan.complete:
                await self._retry_with_workflow(
                    lease,
                    workflow_id,
                    status=(
                        WorkflowStageStatus.FAILED
                        if lease.attempts >= self._max_attempts
                        else WorkflowStageStatus.WAITING_EXTERNAL
                    ),
                    reason="strm_scan_incomplete",
                    error_code="scan_incomplete",
                    max_attempts=self._max_attempts,
                )
                return True
            if strm_linkage_enabled:
                operation = await self._operations.create(
                    library_id=library_id,
                    source_scan_run_id=scan.run_id,
                    kind=StrmOperationKind.INCREMENTAL,
                    workflow_id=workflow_id,
                    idempotency_key=_dirty_operation_idempotency_key(
                        lease, scan.run_id
                    ),
                )
                if operation.status in {"failed", "timeout", "cancelled"}:
                    operation = await self._operations.resume(operation.operation_id)
                operation, acquired = await self._operations.claim_start(
                    operation.operation_id
                )
                if not acquired:
                    if operation.status == "succeeded":
                        await self._sync_workflow(
                            workflow_id,
                            lease,
                            status=WorkflowStageStatus.SUCCEEDED,
                            reason="strm_reused_completed",
                        )
                    else:
                        await self._retry_with_workflow(
                            lease,
                            workflow_id,
                            status=(
                                WorkflowStageStatus.FAILED
                                if lease.attempts >= self._max_attempts
                                else WorkflowStageStatus.WAITING_EXTERNAL
                            ),
                            reason="strm_operation_in_progress",
                            error_code="strm_operation_in_progress",
                            max_attempts=self._max_attempts,
                        )
                        return True
                else:
                    operation_lease_owner = await self._operations.get_lease_token(
                        operation.operation_id
                    )
                    if operation_lease_owner is None:
                        raise StrmOperationError("strm_operation_lease_required")
                    heartbeat_stop, heartbeat_task = self._start_operation_heartbeat(
                        operation.operation_id, operation_lease_owner
                    )

                    async def lease_check() -> bool:
                        return await self._operations.is_lease_active(
                            operation.operation_id,
                            lease_owner=operation_lease_owner,
                        )

                    async def progress_callback(
                        progress: StrmGenerationSummary,
                    ) -> None:
                        await self._operations.progress(
                            operation.operation_id,
                            generated=progress.generated,
                            unchanged=progress.unchanged,
                            skipped=progress.skipped,
                            failed=progress.failed,
                            retired=progress.retired,
                            lease_owner=operation_lease_owner,
                        )

                    summary = await self._strm.incremental(
                        library_id,
                        source_scan_run_id=scan.run_id,
                        output_root=self._output_root,
                        playback_url_prefix=self._playback_url_prefix,
                        # Incremental reconciliation only creates/updates current
                        # entries. Retirement is a separate reviewed cleanup plan.
                        retire_removed=False,
                        lease_check=lease_check,
                        operation_id=operation.operation_id,
                        progress_callback=progress_callback,
                    )
                    operation = await self._finish_operation(
                        operation,
                        summary,
                        lease_owner=operation_lease_owner,
                    )
                    if operation.status != "succeeded":
                        error_code = operation.error_code or "strm_operation_failed"
                        await self._retry_with_workflow(
                            lease,
                            workflow_id,
                            status=(
                                WorkflowStageStatus.FAILED
                                if lease.attempts >= self._max_attempts
                                else WorkflowStageStatus.WAITING_EXTERNAL
                            ),
                            reason="strm_reconcile_failed",
                            error_code=error_code,
                            max_attempts=self._max_attempts,
                        )
                        return True
                    await self._sync_workflow(
                        workflow_id,
                        lease,
                        status=(
                            WorkflowStageStatus.FAILED
                            if operation.status == "failed"
                            else WorkflowStageStatus.SUCCEEDED
                        ),
                        reason="strm_finished",
                        error_code=operation.error_code,
                    )
            if cleanup_empty_directories:
                candidates = await self._cleanup_candidates(
                    scan.run_id, root_directory_id, actions_json
                )
                if candidates:
                    await self._audit(
                        "library.empty_directory_cleanup.review_required",
                        "受管空目录清理需要预览并确认",
                    )
            completed = await self._outbox.complete(self._session_factory, lease)
            if not completed:
                return True
            await self._audit("strm.dirty_consumed", "目录变更已完成增量对账")
        except asyncio.CancelledError:
            raise
        except (LibraryIndexError, StrmManifestError, StrmOperationError) as error:
            await self._fail_operation(
                operation,
                operation_lease_owner,
                error_code=getattr(error, "code", str(error)),
            )
            await self._retry_with_workflow(
                lease,
                workflow_id,
                status=(
                    WorkflowStageStatus.FAILED
                    if lease.attempts >= self._max_attempts
                    else WorkflowStageStatus.WAITING_EXTERNAL
                ),
                reason="strm_reconcile_failed",
                error_code="reconcile_failed",
                max_attempts=self._max_attempts,
            )
        except Exception:  # noqa: BLE001 - details never cross the worker boundary
            await self._fail_operation(
                operation,
                operation_lease_owner,
                error_code="worker_failed",
            )
            await self._retry_with_workflow(
                lease,
                workflow_id,
                status=(
                    WorkflowStageStatus.FAILED
                    if lease.attempts >= self._max_attempts
                    else WorkflowStageStatus.WAITING_EXTERNAL
                ),
                reason="strm_worker_failed",
                error_code="worker_failed",
                max_attempts=self._max_attempts,
            )
        finally:
            if heartbeat_stop is not None and heartbeat_task is not None:
                await self._stop_operation_heartbeat(heartbeat_stop, heartbeat_task)
        return True

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or self._stop
        while not stop.is_set():
            claimed = await self.run_once()
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue

    async def _finish_operation(
        self,
        operation: StrmOperationSummary,
        summary,
        *,
        lease_owner: str,
    ) -> StrmOperationSummary:
        counts = {
            "generated": int(getattr(summary, "generated", 0)),
            "unchanged": int(getattr(summary, "unchanged", 0)),
            "skipped": int(getattr(summary, "skipped", 0)),
            "failed": int(getattr(summary, "failed", 0)),
            "retired": int(getattr(summary, "retired", 0)),
        }
        if counts["failed"]:
            return await self._operations.fail(
                operation.operation_id,
                error_code="strm_incremental_failed",
                lease_owner=lease_owner,
                **counts,
            )
        return await self._operations.complete(
            operation.operation_id,
            lease_owner=lease_owner,
            **counts,
        )

    async def _fail_operation(
        self,
        operation: StrmOperationSummary | None,
        lease_owner: str | None,
        *,
        error_code: str,
    ) -> None:
        if operation is None or lease_owner is None:
            return
        try:
            await self._operations.fail(
                operation.operation_id,
                error_code=error_code,
                lease_owner=lease_owner,
            )
        except (StrmOperationError, StrmOperationNotFound):
            # A newer executor may already own or have terminalized the row.
            # The lease predicate is the fencing boundary in that case.
            return

    def _start_operation_heartbeat(
        self, operation_id: str, lease_owner: str
    ) -> tuple[asyncio.Event, asyncio.Task[None]]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._run_operation_heartbeat(operation_id, lease_owner, stop),
            name=f"watch-assistant-dirty-strm-heartbeat-{operation_id}",
        )
        return stop, task

    async def _run_operation_heartbeat(
        self, operation_id: str, lease_owner: str, stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                try:
                    await self._operations.heartbeat(
                        operation_id, lease_owner=lease_owner
                    )
                except (StrmOperationError, StrmOperationNotFound):
                    return

    async def _stop_operation_heartbeat(
        self, stop: asyncio.Event, task: asyncio.Task[None]
    ) -> None:
        stop.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _load_context(
        self, lease: DirectoryDirtyLease
    ) -> tuple[str, str, str, str | None] | None:
        async with self._session_factory() as session:
            row = await session.execute(
                select(
                    MediaLibrary.id,
                    MediaLibrary.root_directory_id,
                    OrganizationPlan.actions_json,
                    OrganizationOperation.workflow_id,
                )
                .join(OrganizationPlan, OrganizationPlan.library_id == MediaLibrary.id)
                .join(OrganizationOperation, OrganizationOperation.plan_id == OrganizationPlan.id)
                .join(DirectoryDirtyEvent, DirectoryDirtyEvent.operation_id == OrganizationOperation.id)
                .where(
                    DirectoryDirtyEvent.id == lease.event_id,
                    MediaLibrary.enabled.is_(True),
                    MediaLibrary.scope_verified.is_(True),
                )
            )
            item = row.one_or_none()
        if item is None:
            return None
        library_id, root_directory_id, actions_json, workflow_id = item
        if not _directory_in_plan_scope(lease.directory_id, root_directory_id, actions_json):
            return None
        return library_id, root_directory_id, actions_json, workflow_id

    async def _retry_with_workflow(
        self,
        lease: DirectoryDirtyLease,
        workflow_id: str | None,
        *,
        status: WorkflowStageStatus,
        reason: str,
        error_code: str,
        max_attempts: int,
    ) -> bool:
        workflow_event: tuple[str, str] | None = None

        async def sync_before_release(session: AsyncSession) -> None:
            nonlocal workflow_event
            if workflow_id is None:
                return
            try:
                workflow = await sync_child_stage(
                    session,
                    workflow_id,
                    WorkflowStageName.STRM,
                    child_type="strm_dirty_generation",
                    child_id=lease.queue_id or lease.event_id,
                    status=status,
                    reason=reason,
                    error_code=error_code,
                )
            except (WorkflowConflict, WorkflowNotFound):
                return
            workflow_event = (workflow.correlation_id, workflow.id)

        retried = await self._outbox.retry(
            self._session_factory,
            lease,
            error_code=error_code,
            max_attempts=max_attempts,
            before_release=sync_before_release,
        )
        if retried and workflow_event is not None:
            await emit_event(
                self._event_logger,
                "workflow.stage_changed",
                fields={
                    "status": status.value,
                    "stage": WorkflowStageName.STRM.value,
                },
                correlation_id=workflow_event[0],
                task_id=workflow_event[1],
            )
        return retried

    async def _sync_workflow(
        self,
        workflow_id: str | None,
        lease: DirectoryDirtyLease,
        *,
        status: WorkflowStageStatus,
        reason: str,
        error_code: str | None = None,
    ) -> bool:
        if workflow_id is None:
            return False
        try:
            current = datetime.now(UTC)
            async with self._session_factory() as session:
                if lease.queue_id is not None:
                    queue_filters = [
                        DirectoryDirtyGeneration.id == lease.queue_id,
                        DirectoryDirtyGeneration.status == GENERATION_RUNNING,
                        DirectoryDirtyGeneration.lease_token == lease.lease_token,
                        DirectoryDirtyGeneration.lease_expires_at > current,
                    ]
                    if lease.generation is not None:
                        queue_filters.append(
                            DirectoryDirtyGeneration.generation == lease.generation
                        )
                    queue_result = await session.execute(
                        update(DirectoryDirtyGeneration)
                        .where(*queue_filters)
                        .values(updated_at=current)
                        .execution_options(synchronize_session=False)
                    )
                    if queue_result.rowcount != 1:
                        await session.rollback()
                        return False
                event_result = await session.execute(
                    update(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.id == lease.event_id,
                        DirectoryDirtyEvent.status == DIRTY_RUNNING,
                        DirectoryDirtyEvent.lease_token == lease.lease_token,
                        DirectoryDirtyEvent.lease_expires_at > current,
                    )
                    .values(updated_at=current)
                    .execution_options(synchronize_session=False)
                )
                if event_result.rowcount != 1:
                    await session.rollback()
                    return False
                workflow = await sync_child_stage(
                    session,
                    workflow_id,
                    WorkflowStageName.STRM,
                    child_type="strm_dirty_generation",
                    child_id=lease.queue_id or lease.event_id,
                    status=status,
                    reason=reason,
                    error_code=error_code,
                )
                await session.commit()
        except WorkflowNotFound:
            return False
        await emit_event(
            self._event_logger,
            "workflow.stage_changed",
            fields={"status": status.value, "stage": WorkflowStageName.STRM.value},
            correlation_id=workflow.correlation_id,
            task_id=workflow.id,
        )
        return True

    async def _cleanup_candidates(
        self, scan_run_id: str, root_directory_id: str, actions_json: str
    ) -> tuple[_CleanupCandidate, ...]:
        source_parent_ids = _source_parent_ids(actions_json)
        if not source_parent_ids:
            return ()
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id
                        )
                    )
                ).all()
            )
        directories = {
            row.object_id: row
            for row in rows
            if row.is_directory and row.object_id in source_parent_ids
        }
        occupied = {row.parent_id for row in rows if row.parent_id is not None}
        return tuple(
            _CleanupCandidate(row.object_id, row.parent_id, row.name)
            for row in sorted(directories.values(), key=lambda item: item.object_id)
            if row.object_id != root_directory_id
            and isinstance(row.parent_id, str)
            and row.object_id not in occupied
        )

    async def _audit(self, event: str, status: str) -> None:
        logger = self._event_logger
        log_event = getattr(logger, "log_event", None)
        if not callable(log_event):
            return
        try:
            await log_event(event, fields={"status": status})
        except Exception:  # noqa: BLE001 - audit failure cannot block reconciliation
            return


def _directory_in_plan_scope(directory_id: str, root_directory_id: str, actions_json: str) -> bool:
    if directory_id == root_directory_id:
        return True
    try:
        payload = json.loads(actions_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, list):
        return False
    for action in payload:
        if not isinstance(action, dict):
            continue
        for record in (action, action.get("execution")):
            if not isinstance(record, dict):
                continue
            for key in (
                "source_parent_id",
                "target_parent_id",
                "source_directory_id",
                "target_directory_id",
            ):
                if record.get(key) == directory_id:
                    return True
            members = record.get("members")
            if not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, dict) and any(
                    member.get(key) == directory_id
                    for key in ("source_parent_id", "target_parent_id")
                ):
                    return True
    return False


def _dirty_idempotency_key(lease: DirectoryDirtyLease) -> str:
    scope = lease.queue_id or lease.event_id
    if lease.generation is not None:
        scope = f"{scope}-{lease.generation}"
    return f"dirty-{scope}"


def _dirty_operation_idempotency_key(
    lease: DirectoryDirtyLease, source_scan_run_id: str
) -> str:
    scope = "|".join(
        (
            lease.queue_id or lease.event_id,
            str(lease.generation if lease.generation is not None else 0),
            source_scan_run_id,
        )
    )
    return "dirty-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def _source_parent_ids(actions_json: str) -> set[str]:
    try:
        actions = json.loads(actions_json)
    except (TypeError, ValueError):
        return set()
    if not isinstance(actions, list):
        return set()
    result: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        members = action.get("members")
        if not isinstance(members, list):
            execution = action.get("execution")
            members = execution.get("members") if isinstance(execution, dict) else None
        if not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, dict) and isinstance(member.get("source_parent_id"), str):
                result.add(member["source_parent_id"])
    return result


__all__ = ["DirectoryDirtyWorker"]
