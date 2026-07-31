"""Durable, local-only state and leases for planned organization operations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    OrganizationOperation,
    OrganizationOperationStatus,
    Workflow,
)
from watch_assistant.schemas import WorkflowStageName, WorkflowStageStatus
from watch_assistant.services.library_index import ScanRunState
from watch_assistant.services.organization_outbox import (
    DirectoryDirtyOutboxService,
    OrganizationOutboxError,
)
from watch_assistant.services.organization_plan import OrganizationPlanStatus
from watch_assistant.services.workflows import sync_child_stage

VALID_OPERATION_ERROR_CODES = frozenset(
    {
        "cancelled",
        "lease_lost",
        "local_failure",
        "outcome_unknown",
        "postcondition_mismatch",
        "plan_prerequisites_changed",
        "rate_limited",
        "remote_write_failed",
        "timeout",
    }
)


class OrganizationOperationNotFound(LookupError):
    pass


class OrganizationOperationConflict(ValueError):
    pass


class OrganizationOperationStateError(ValueError):
    pass


class OrganizationOperationPrerequisiteError(ValueError):
    pass


class OrganizationOperationLeaseUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationOperationSummary:
    operation_id: str
    plan_id: str
    status: OrganizationOperationStatus
    revision: int
    attempts: int
    error_code: str | None
    workflow_id: str | None
    cancel_requested: bool

    def __repr__(self) -> str:
        return (
            "OrganizationOperationSummary(operation_id=<redacted>, "
            "plan_id=<redacted>, "
            f"status={self.status.value!r}, revision={self.revision}, "
            f"attempts={self.attempts}, error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationOperationLease:
    operation_id: str
    revision: int
    lease_token: str
    lease_expires_at: datetime

    def __repr__(self) -> str:
        return (
            "OrganizationOperationLease(operation_id=<redacted>, "
            f"revision={self.revision}, lease_token=<redacted>)"
        )


class OrganizationOperationService:
    """Manage local operation state only; no transport or remote write exists."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: object | None = None,
        outbox_service: DirectoryDirtyOutboxService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger
        self._outbox_service = outbox_service or DirectoryDirtyOutboxService()

    async def create(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        expected_plan_revision: int | None = None,
        workflow_id: str | None = None,
    ) -> OrganizationOperationSummary:
        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        _validate_identifier(idempotency_key, "invalid_idempotency_key", maximum=255)
        if workflow_id is not None:
            _validate_identifier(workflow_id, "invalid_workflow_id", maximum=40)
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            await self._ensure_planned_and_current(session, plan)
            workflow = None
            if workflow_id is not None:
                workflow = await session.get(Workflow, workflow_id)
                if workflow is None:
                    raise OrganizationOperationPrerequisiteError("workflow_not_found")
            if (
                expected_plan_revision is not None
                and plan.revision != expected_plan_revision
            ):
                raise OrganizationOperationConflict("plan_revision_changed")

            existing = await session.scalar(
                select(OrganizationOperation).where(
                    OrganizationOperation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.plan_id != plan_id:
                    raise OrganizationOperationConflict("idempotency_key_conflict")
                if workflow_id is not None and existing.workflow_id != workflow_id:
                    raise OrganizationOperationConflict("workflow_id_conflict")
                return _summary(existing)

            existing = await session.scalar(
                select(OrganizationOperation).where(
                    OrganizationOperation.plan_id == plan_id
                )
            )
            if existing is not None:
                raise OrganizationOperationConflict("operation_plan_conflict")
            operation = OrganizationOperation(
                id="op_" + uuid.uuid4().hex,
                plan_id=plan.id,
                workflow_id=workflow_id,
                plan_revision=plan.revision,
                idempotency_key=idempotency_key,
                status=OrganizationOperationStatus.PLANNED,
                revision=1,
                attempts=0,
            )
            session.add(operation)
            if workflow is not None:
                await _sync_workflow_stage(
                    session,
                    workflow.id,
                    status=WorkflowStageStatus.PENDING,
                    child_id=operation.id,
                    reason="organization_queued",
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(OrganizationOperation).where(
                        OrganizationOperation.idempotency_key == idempotency_key
                    )
                )
                if existing is not None and existing.plan_id == plan_id:
                    return _summary(existing)
                existing = await session.scalar(
                    select(OrganizationOperation).where(
                        OrganizationOperation.plan_id == plan_id
                    )
                )
                if existing is not None:
                    raise OrganizationOperationConflict(
                        "operation_plan_conflict"
                    ) from None
                raise OrganizationOperationConflict(
                    "operation_creation_conflict"
                ) from None
            summary = _summary(operation)
        await self._audit("organize.operation.queued", "整理操作已排队")
        return summary

    async def get(self, operation_id: str) -> OrganizationOperationSummary:
        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            return _summary(operation)

    async def get_for_plan(
        self, plan_id: str
    ) -> OrganizationOperationSummary | None:
        """Return the single durable operation associated with a plan, if any."""

        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        async with self._session_factory() as session:
            operation = await session.scalar(
                select(OrganizationOperation)
                .where(OrganizationOperation.plan_id == plan_id)
                .limit(1)
            )
            return _summary(operation) if operation is not None else None

    async def cancel_requested(self, operation_id: str) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(OrganizationOperation.cancel_requested).where(
                    OrganizationOperation.id == operation_id
                )
            )
        return value is True

    async def claim_next(
        self,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> OrganizationOperationLease | None:
        """Claim the oldest approved operation, if one is available."""

        async with self._session_factory() as session:
            operation = await session.scalar(
                select(OrganizationOperation)
                .where(OrganizationOperation.status == OrganizationOperationStatus.PLANNED)
                .order_by(OrganizationOperation.created_at.asc(), OrganizationOperation.id.asc())
                .limit(1)
            )
            if operation is None:
                return None
            operation_id = operation.id
            revision = operation.revision
        try:
            return await self.claim(
                operation_id,
                expected_revision=revision,
                lease_duration=lease_duration,
                now=now,
            )
        except OrganizationOperationLeaseUnavailable:
            return None

    async def plan_execution_scope(self, operation_id: str) -> frozenset[str] | None:
        """Return the complete directory scope frozen by the approved plan."""

        from watch_assistant.services.organization_plan import load_executable_steps

        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                return None
            steps = await load_executable_steps(self._session_factory, operation.plan_id)
        if not steps:
            return None
        return frozenset(
            directory_id
            for step in steps
            for directory_id in step.scope_directory_ids
        )

    async def load_execution_steps(self, operation_id: str):
        """Load steps again after claim, so execution uses durable plan data."""

        from watch_assistant.services.organization_plan import load_executable_steps

        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                return None
            return await load_executable_steps(self._session_factory, operation.plan_id)

    async def plan_digest(self, plan_id: str) -> str:
        """Return the immutable digest used by an explicit execution confirmation."""
        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationOperationNotFound
            return plan.plan_hash

    async def claim(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> OrganizationOperationLease:
        duration = _validate_lease_duration(lease_duration)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            operation = await self._load_operation(
                session, operation_id, expected_revision
            )
            plan = await session.get(OrganizationPlan, operation.plan_id)
            await self._ensure_planned_and_current(session, plan)
            if plan.revision != operation.plan_revision:
                raise OrganizationOperationPrerequisiteError("plan_revision_changed")
            if operation.status is OrganizationOperationStatus.UNCERTAIN:
                raise OrganizationOperationStateError("uncertain_requires_verification")
            if operation.status not in {
                OrganizationOperationStatus.PLANNED,
                OrganizationOperationStatus.ORGANIZING,
            }:
                raise OrganizationOperationStateError("operation_is_not_claimable")
            if (
                operation.status is OrganizationOperationStatus.ORGANIZING
                and operation.lease_expires_at is not None
                and _as_utc(operation.lease_expires_at) > current_time
            ):
                raise OrganizationOperationLeaseUnavailable("lease_is_active")

            token = uuid.uuid4().hex
            expires_at = current_time + duration
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    or_(
                        OrganizationOperation.status
                        == OrganizationOperationStatus.PLANNED,
                        (
                            OrganizationOperation.status
                            == OrganizationOperationStatus.ORGANIZING
                        )
                        & (OrganizationOperation.lease_expires_at <= current_time),
                    ),
                )
                .values(
                    status=OrganizationOperationStatus.ORGANIZING,
                    revision=expected_revision + 1,
                    attempts=OrganizationOperation.attempts + 1,
                    lease_token=token,
                    lease_expires_at=expires_at,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationLeaseUnavailable("lease_claim_lost")
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=WorkflowStageStatus.RUNNING,
                child_id=operation.id,
                reason="organization_started",
            )
            await session.commit()
            return OrganizationOperationLease(
                operation_id=operation_id,
                revision=expected_revision + 1,
                lease_token=token,
                lease_expires_at=expires_at,
            )

    async def renew_lease(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_token: str,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> OrganizationOperationLease:
        duration = _validate_lease_duration(lease_duration)
        _validate_token(lease_token)
        current_time = _as_utc(now or datetime.now(UTC))
        expires_at = current_time + duration
        async with self._session_factory() as session:
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.ORGANIZING,
                    OrganizationOperation.lease_token == lease_token,
                    OrganizationOperation.lease_expires_at > current_time,
                )
                .values(
                    revision=expected_revision + 1,
                    lease_expires_at=expires_at,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
            await session.commit()
            return OrganizationOperationLease(
                operation_id=operation_id,
                revision=expected_revision + 1,
                lease_token=lease_token,
                lease_expires_at=expires_at,
            )

    async def finish(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_token: str,
        status: OrganizationOperationStatus,
        error_code: str | None = None,
        source_directory_id: str | None = None,
        target_directory_id: str | None = None,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        if status not in {
            OrganizationOperationStatus.ORGANIZED,
            OrganizationOperationStatus.FAILED,
            OrganizationOperationStatus.UNCERTAIN,
            OrganizationOperationStatus.CANCELLED,
        }:
            raise OrganizationOperationStateError("invalid_terminal_status")
        if status is OrganizationOperationStatus.ORGANIZED:
            if source_directory_id is None or target_directory_id is None:
                raise OrganizationOperationStateError("directory_scope_required")
            return await self.complete_organized_with_dirty_events(
                operation_id,
                expected_revision=expected_revision,
                lease_token=lease_token,
                source_directory_id=source_directory_id,
                target_directory_id=target_directory_id,
                now=now,
            )
        _validate_token(lease_token)
        _validate_error_code(error_code)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.ORGANIZING,
                    OrganizationOperation.lease_token == lease_token,
                    OrganizationOperation.lease_expires_at > current_time,
                )
                .values(
                    status=status,
                    revision=expected_revision + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=error_code,
                    finished_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            if (
                status is OrganizationOperationStatus.FAILED
                and error_code == "plan_prerequisites_changed"
            ):
                plan = await session.get(OrganizationPlan, operation.plan_id)
                if plan is not None and plan.status == OrganizationPlanStatus.PLANNED.value:
                    plan.status = OrganizationPlanStatus.INVALIDATED.value
                    plan.revision += 1
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=_workflow_stage_status(status),
                child_id=operation.id,
                reason="organization_finished",
                error_code=error_code,
            )
            await session.commit()
            summary = _summary(operation)
        await self._audit("organize.operation.updated", "整理操作状态已更新")
        return summary

    async def finish_after_lease_loss(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_token: str,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        """Persist uncertainty after a write when the lease cannot renew."""

        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        _validate_token(lease_token)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.ORGANIZING,
                    OrganizationOperation.lease_token == lease_token,
                )
                .values(
                    status=OrganizationOperationStatus.UNCERTAIN,
                    revision=expected_revision + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code="lease_lost",
                    finished_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=WorkflowStageStatus.UNCERTAIN,
                child_id=operation.id,
                reason="organization_uncertain",
                error_code="lease_lost",
            )
            await session.commit()
            summary = _summary(operation)
        await self._audit("organize.operation.uncertain", "整理操作已标记为结果不确定")
        return summary

    async def complete_organized_with_dirty_events(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_token: str,
        source_directory_id: str,
        target_directory_id: str,
        directory_ids: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        """Complete a valid lease and enqueue dirty events in one transaction."""

        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        _validate_token(lease_token)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    update(OrganizationOperation)
                    .where(
                        OrganizationOperation.id == operation_id,
                        OrganizationOperation.revision == expected_revision,
                        OrganizationOperation.status
                        == OrganizationOperationStatus.ORGANIZING,
                        OrganizationOperation.lease_token == lease_token,
                        OrganizationOperation.lease_expires_at > current_time,
                    )
                    .values(
                        status=OrganizationOperationStatus.ORGANIZED,
                        revision=expected_revision + 1,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code=None,
                        finished_at=current_time,
                        updated_at=current_time,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
                await self._outbox_service.enqueue_directory_dirty(
                    session,
                    operation_id=operation_id,
                    directory_ids=(
                        (source_directory_id, target_directory_id)
                        if directory_ids is None
                        else directory_ids
                    ),
                )
                operation = await session.get(OrganizationOperation, operation_id)
                if operation is None:
                    raise OrganizationOperationNotFound
                await _sync_workflow_stage(
                    session,
                    operation.workflow_id,
                    status=WorkflowStageStatus.SUCCEEDED,
                    child_id=operation.id,
                    reason="organization_finished",
                )
                await session.commit()
            except OrganizationOperationLeaseUnavailable:
                await session.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 - rollback and map locally
                await session.rollback()
                if isinstance(exc, OrganizationOutboxError):
                    raise OrganizationOperationConflict(str(exc)) from None
                raise OrganizationOperationConflict(
                    "outbox_persistence_failed"
                ) from None
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            summary = _summary(operation)
        await self._audit("organize.operation.completed", "整理操作已完成")
        return summary

    async def cancel(
        self, operation_id: str, *, expected_revision: int
    ) -> OrganizationOperationSummary:
        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        current_time = datetime.now(UTC)
        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            if operation.revision != expected_revision:
                raise OrganizationOperationConflict("operation_revision_changed")
            if operation.status is OrganizationOperationStatus.ORGANIZING:
                if operation.cancel_requested:
                    return _summary(operation)
                operation.cancel_requested = True
                operation.updated_at = current_time
                await session.commit()
                summary = _summary(operation)
                await self._audit(
                    "organize.operation.cancel_requested", "整理操作已请求本地中止"
                )
                return summary
            if operation.status is not OrganizationOperationStatus.PLANNED:
                raise OrganizationOperationStateError("operation_is_not_cancellable")
            operation.status = OrganizationOperationStatus.CANCELLED
            operation.revision = expected_revision + 1
            operation.finished_at = current_time
            operation.updated_at = current_time
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=WorkflowStageStatus.CANCELLED,
                child_id=operation.id,
                reason="organization_cancelled",
            )
            await session.commit()
            summary = _summary(operation)
        await self._audit("organize.operation.cancelled", "整理操作已取消")
        return summary

    async def retry(
        self, operation_id: str, *, expected_revision: int
    ) -> OrganizationOperationSummary:
        """Reset a confirmed local failure; uncertain outcomes need verification."""

        current_time = datetime.now(UTC)
        async with self._session_factory() as session:
            operation = await self._load_operation(
                session, operation_id, expected_revision
            )
            if operation.status is OrganizationOperationStatus.UNCERTAIN:
                raise OrganizationOperationStateError("uncertain_requires_verification")
            if operation.status is not OrganizationOperationStatus.FAILED:
                raise OrganizationOperationStateError("operation_is_not_retryable")
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status == OrganizationOperationStatus.FAILED,
                )
                .values(
                    status=OrganizationOperationStatus.PLANNED,
                    revision=expected_revision + 1,
                    cancel_requested=False,
                    error_code=None,
                    finished_at=None,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationConflict("operation_revision_changed")
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=WorkflowStageStatus.PENDING,
                child_id=operation.id,
                reason="organization_retried",
            )
            await session.commit()
            await session.refresh(operation)
            summary = _summary(operation)
        await self._audit("organize.operation.retried", "整理操作已重试")
        return summary

    async def _load_operation(
        self, session: AsyncSession, operation_id: str, expected_revision: int
    ) -> OrganizationOperation:
        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        operation = await session.get(OrganizationOperation, operation_id)
        if operation is None:
            raise OrganizationOperationNotFound
        if operation.revision != expected_revision:
            raise OrganizationOperationConflict("operation_revision_changed")
        return operation

    async def _ensure_planned_and_current(
        self, session: AsyncSession, plan: OrganizationPlan | None
    ) -> None:
        if plan is None:
            raise OrganizationOperationPrerequisiteError("plan_not_found")
        if plan.status != OrganizationPlanStatus.PLANNED.value:
            raise OrganizationOperationPrerequisiteError("plan_is_not_planned")
        library = await session.get(MediaLibrary, plan.library_id)
        run = await session.get(LibraryScanRun, plan.source_scan_run_id)
        current = (
            library is not None
            and library.enabled
            and library.scope_verified
            and _plan_library_snapshot_matches(plan, library)
            and run is not None
            and run.library_id == plan.library_id
            and run.root_directory_id == library.root_directory_id
            and run.state == ScanRunState.COMPLETED.value
            and run.complete
            and run.snapshot_revision == plan.source_snapshot_revision
            and _as_utc(plan.expires_at) > datetime.now(UTC)
        )
        if current:
            return
        if plan.status == OrganizationPlanStatus.PLANNED.value:
            plan.status = OrganizationPlanStatus.INVALIDATED.value
            plan.revision += 1
            await session.commit()
        raise OrganizationOperationPrerequisiteError("plan_prerequisites_changed")

    async def _audit(self, event: str, status: str) -> None:
        logger = self._event_logger
        log_event = getattr(logger, "log_event", None)
        if not callable(log_event):
            return
        try:
            await log_event(event, fields={"status": status})
        except Exception:  # noqa: BLE001 - audit failure cannot alter local state
            return


def _summary(operation: OrganizationOperation) -> OrganizationOperationSummary:
    return OrganizationOperationSummary(
        operation_id=operation.id,
        plan_id=operation.plan_id,
        status=operation.status,
        revision=operation.revision,
        attempts=operation.attempts,
        error_code=operation.error_code,
        workflow_id=operation.workflow_id,
        cancel_requested=operation.cancel_requested,
    )


async def _sync_workflow_stage(
    session: AsyncSession,
    workflow_id: str | None,
    *,
    status: WorkflowStageStatus,
    child_id: str,
    reason: str,
    error_code: str | None = None,
) -> None:
    if workflow_id is None:
        return
    await sync_child_stage(
        session,
        workflow_id,
        WorkflowStageName.ORGANIZATION,
        child_type="organization_operation",
        child_id=child_id,
        status=status,
        reason=reason,
        error_code=error_code,
    )


def _workflow_stage_status(status: OrganizationOperationStatus) -> WorkflowStageStatus:
    return {
        OrganizationOperationStatus.FAILED: WorkflowStageStatus.FAILED,
        OrganizationOperationStatus.UNCERTAIN: WorkflowStageStatus.UNCERTAIN,
        OrganizationOperationStatus.CANCELLED: WorkflowStageStatus.CANCELLED,
    }[status]


def _plan_library_snapshot_matches(
    plan: OrganizationPlan, library: MediaLibrary
) -> bool:
    try:
        preconditions = json.loads(plan.preconditions_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(preconditions, dict):
        return False
    return preconditions.get("library") == {
        "library_id": library.id,
        "revision": library.revision,
        "enabled": library.enabled,
        "scope_verified": library.scope_verified,
        "root_directory_id": library.root_directory_id,
    }


def _validate_identifier(value: str, error: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in ("/", "\\", "://", "\x00"))
    ):
        raise ValueError(error)


def _validate_token(value: str) -> None:
    if not isinstance(value, str) or len(value) != 32 or not value.isascii():
        raise ValueError("invalid_lease_token")


def _validate_error_code(value: str | None) -> None:
    if value is not None and value not in VALID_OPERATION_ERROR_CODES:
        raise ValueError("invalid_error_code")


def _validate_lease_duration(value: timedelta) -> timedelta:
    if value <= timedelta() or value > timedelta(hours=1):
        raise ValueError("invalid_lease_duration")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "VALID_OPERATION_ERROR_CODES",
    "OrganizationOperationConflict",
    "OrganizationOperationLease",
    "OrganizationOperationLeaseUnavailable",
    "OrganizationOperationNotFound",
    "OrganizationOperationPrerequisiteError",
    "OrganizationOperationService",
    "OrganizationOperationStateError",
    "OrganizationOperationSummary",
]
