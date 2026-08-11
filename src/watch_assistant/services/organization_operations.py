"""Durable, local-only state and leases for planned organization operations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanRun,
    MediaLibrary,
    OrganizationHistoryEntry,
    OrganizationPlan,
)
from watch_assistant.models import (
    OrganizationOperation,
    OrganizationOperationStatus,
    Workflow,
)
from watch_assistant.schemas import WorkflowStageName, WorkflowStageStatus
from watch_assistant.services.library_index import ScanRunState
from watch_assistant.services.observability import audit_event
from watch_assistant.services.organization_outbox import (
    DirectoryDirtyOutboxService,
    OrganizationOutboxError,
)
from watch_assistant.services.organization_plan import OrganizationPlanStatus
from watch_assistant.services.strm_scope import source_snapshot_is_current
from watch_assistant.services.workflows import sync_child_stage

VALID_OPERATION_ERROR_CODES = frozenset(
    {
        "cancelled",
        "capability_unverified",
        "cleanup_postcondition_mismatch",
        "contract_unverified",
        "lease_lost",
        "local_failure",
        "organization_lease_required",
        "outcome_unknown",
        "postcondition_mismatch",
        "approval_required",
        "permanent_delete_disabled",
        "plan_not_executable",
        "plan_prerequisites_changed",
        "rate_limited",
        "remote_write_failed",
        "replacement_already_removed",
        "scope_unverified",
        "target_directory_create_failed",
        "directory_ownership_unavailable",
        "directory_ownership_unrecorded",
        "target_directory_parent_missing",
        "target_root_changed",
        "timeout",
        "write_disabled",
    }
)

# Failure codes that mean the plan itself is no longer trustworthy. Any other
# failure keeps the plan planned so the operation can be retried against the
# same frozen plan (e.g. target_root_changed is a configuration mismatch, not
# evidence that the plan is stale).
_PLAN_INVALIDATING_ERROR_CODES = frozenset(
    {
        "plan_prerequisites_changed",
        "source_snapshot_changed",
    }
)

# Statuses that keep a plan locked: a new operation may only be created once
# every previous operation reached a terminal state (organized / failed /
# cancelled). An uncertain operation still blocks creation because its outcome
# must be resolved via reconciliation before the plan can be re-run.
_ACTIVE_OPERATION_STATUSES = frozenset(
    {
        OrganizationOperationStatus.PLANNED,
        OrganizationOperationStatus.ORGANIZING,
        OrganizationOperationStatus.UNCERTAIN,
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


class _OrganizationCompletionScopeError(OrganizationOperationConflict):
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
            existing = await session.scalar(
                select(OrganizationOperation).where(
                    OrganizationOperation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return _existing_operation_summary(
                    existing,
                    plan_id=plan_id,
                    workflow_id=workflow_id,
                )
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

            if not await self._has_executable_steps(
                plan_id, expected_plan_revision=plan.revision
            ):
                raise OrganizationOperationPrerequisiteError("plan_not_executable")

            if await _plan_has_active_operation(session, plan_id):
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
                    if workflow_id is not None and existing.workflow_id != workflow_id:
                        raise OrganizationOperationConflict("workflow_id_conflict")
                    return _summary(existing)
                if await _plan_has_active_operation(session, plan_id):
                    raise OrganizationOperationConflict(
                        "operation_plan_conflict"
                    ) from None
                raise OrganizationOperationConflict(
                    "operation_creation_conflict"
                ) from None
            summary = _summary(operation)
        await self._audit("organize.operation.queued", "整理操作已排队")
        return summary

    async def get_by_idempotency_key(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        workflow_id: str | None = None,
    ) -> OrganizationOperationSummary | None:
        """Return an accepted operation before rechecking mutable plan state."""

        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        _validate_identifier(idempotency_key, "invalid_idempotency_key", maximum=255)
        if workflow_id is not None:
            _validate_identifier(workflow_id, "invalid_workflow_id", maximum=40)
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(OrganizationOperation).where(
                    OrganizationOperation.idempotency_key == idempotency_key
                )
            )
            if existing is None:
                return None
            return _existing_operation_summary(
                existing,
                plan_id=plan_id,
                workflow_id=workflow_id,
            )

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
        """Return the newest operation associated with a plan, if any.

        Terminal operations no longer block the plan, so a plan may carry
        several operations (a failed attempt plus its retry). Return the most
        recent one, which represents the current attempt.
        """

        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        async with self._session_factory() as session:
            operation = await session.scalar(
                select(OrganizationOperation)
                .where(OrganizationOperation.plan_id == plan_id)
                .order_by(
                    OrganizationOperation.created_at.desc(),
                    OrganizationOperation.id.desc(),
                )
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

    async def plan_revision_is_current(
        self, operation_id: str, *, expected_operation_revision: int
    ) -> bool:
        """Fence a read-only recovery pass to the operation's frozen plan."""

        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None or operation.revision != expected_operation_revision:
                return False
            plan = await session.get(OrganizationPlan, operation.plan_id)
            return plan is not None and plan.revision == operation.plan_revision

    async def claim_next(
        self,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> OrganizationOperationLease | None:
        """Claim the oldest approved operation, never replaying an expired write."""

        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            expired = list(
                await session.scalars(
                    select(OrganizationOperation).where(
                        OrganizationOperation.status
                        == OrganizationOperationStatus.ORGANIZING,
                        OrganizationOperation.lease_expires_at <= current_time,
                    )
                )
            )
            for operation in expired:
                operation.status = OrganizationOperationStatus.UNCERTAIN
                operation.revision += 1
                operation.lease_token = None
                operation.lease_expires_at = None
                operation.error_code = "outcome_unknown"
                operation.finished_at = current_time
                operation.updated_at = current_time
                await _sync_workflow_stage(
                    session,
                    operation.workflow_id,
                    status=WorkflowStageStatus.UNCERTAIN,
                    child_id=operation.id,
                    reason="organization_uncertain",
                    error_code="outcome_unknown",
                )
            if expired:
                await session.commit()
            operation = await session.scalar(
                select(OrganizationOperation)
                .where(
                    OrganizationOperation.status == OrganizationOperationStatus.PLANNED
                )
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
        except OrganizationOperationPrerequisiteError as exc:
            error_code = str(exc)
            if error_code not in VALID_OPERATION_ERROR_CODES:
                error_code = "plan_prerequisites_changed"
            try:
                await self.fail_planned(
                    operation_id,
                    expected_revision=revision,
                    error_code=error_code,
                )
            except (
                OrganizationOperationLeaseUnavailable,
                OrganizationOperationNotFound,
            ):
                pass
            return None

    async def fail_planned(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        error_code: str,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        """Finish an unclaimed operation that cannot safely be executed."""

        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        _validate_error_code(error_code)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status == OrganizationOperationStatus.PLANNED,
                )
                .values(
                    status=OrganizationOperationStatus.FAILED,
                    revision=expected_revision + 1,
                    error_code=error_code,
                    finished_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise OrganizationOperationLeaseUnavailable("operation_revision_changed")
            await session.commit()
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            summary = _summary(operation)
        await self._audit("organize.operation.failed", "整理操作已失败")
        return summary

    async def plan_execution_scope(
        self,
        operation_id: str,
        *,
        expected_operation_revision: int | None = None,
        expected_plan_revision: int | None = None,
    ) -> frozenset[str] | None:
        """Return the complete directory scope frozen by the approved plan."""

        from watch_assistant.services.organization_plan import load_executable_steps

        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                return None
            if (
                expected_operation_revision is not None
                and operation.revision != expected_operation_revision
            ):
                return None
            if (
                expected_plan_revision is not None
                and operation.plan_revision != expected_plan_revision
            ):
                return None
            steps = await load_executable_steps(
                self._session_factory,
                operation.plan_id,
                expected_plan_revision=operation.plan_revision,
            )
        if not steps:
            return None
        return frozenset(
            directory_id
            for step in steps
            for directory_id in step.scope_directory_ids
        )

    async def load_execution_steps(
        self,
        operation_id: str,
        *,
        expected_operation_revision: int | None = None,
    ):
        """Load steps again after claim, so execution uses durable plan data."""

        from watch_assistant.services.organization_plan import load_executable_steps

        async with self._session_factory() as session:
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                return None
            if (
                expected_operation_revision is not None
                and operation.revision != expected_operation_revision
            ):
                return None
            return await load_executable_steps(
                self._session_factory,
                operation.plan_id,
                expected_plan_revision=operation.plan_revision,
            )

    async def plan_digest(self, plan_id: str) -> str:
        """Return the immutable digest used by an explicit execution confirmation."""
        _validate_identifier(plan_id, "invalid_plan_id", maximum=64)
        async with self._session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_id)
            if plan is None:
                raise OrganizationOperationNotFound
            return plan.plan_hash

    async def _has_executable_steps(
        self, plan_id: str, *, expected_plan_revision: int | None = None
    ) -> bool:
        from watch_assistant.services.organization_plan import load_executable_steps

        steps = await load_executable_steps(
            self._session_factory,
            plan_id,
            expected_plan_revision=expected_plan_revision,
        )
        return bool(steps)

    async def has_executable_steps(
        self, plan_id: str, *, allow_unconfirmed: bool = False
    ) -> bool:
        """Preflight a plan without changing its review status."""

        from watch_assistant.services.organization_plan import load_executable_steps

        steps = await load_executable_steps(
            self._session_factory, plan_id, allow_unconfirmed=allow_unconfirmed
        )
        return bool(steps)

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
            if not await self._has_executable_steps(
                operation.plan_id,
                expected_plan_revision=operation.plan_revision,
            ):
                raise OrganizationOperationPrerequisiteError("plan_not_executable")
            if operation.status is OrganizationOperationStatus.UNCERTAIN:
                raise OrganizationOperationStateError("uncertain_requires_verification")
            if operation.status is OrganizationOperationStatus.ORGANIZING:
                lease_expired = operation.lease_expires_at is None or (
                    _as_utc(operation.lease_expires_at) <= current_time
                )
                if lease_expired:
                    operation.status = OrganizationOperationStatus.UNCERTAIN
                    operation.revision += 1
                    operation.lease_token = None
                    operation.lease_expires_at = None
                    operation.error_code = "outcome_unknown"
                    operation.finished_at = current_time
                    operation.updated_at = current_time
                    await _sync_workflow_stage(
                        session,
                        operation.workflow_id,
                        status=WorkflowStageStatus.UNCERTAIN,
                        child_id=operation.id,
                        reason="organization_uncertain",
                        error_code="outcome_unknown",
                    )
                    await session.commit()
                    await self._audit(
                        "organize.operation.uncertain",
                        "整理租约已过期，操作结果待远端核对",
                    )
                    raise OrganizationOperationStateError(
                        "uncertain_requires_verification"
                    )
            if operation.status not in {
                OrganizationOperationStatus.PLANNED,
                OrganizationOperationStatus.ORGANIZING,
            }:
                raise OrganizationOperationStateError("operation_is_not_claimable")
            if operation.status is OrganizationOperationStatus.ORGANIZING:
                raise OrganizationOperationLeaseUnavailable("lease_is_active")

            token = uuid.uuid4().hex
            expires_at = current_time + duration
            plan_revision = (
                select(OrganizationPlan.revision)
                .where(OrganizationPlan.id == OrganizationOperation.plan_id)
                .scalar_subquery()
            )
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.PLANNED,
                    OrganizationOperation.plan_revision == plan_revision,
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
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            if operation.revision != expected_revision:
                raise OrganizationOperationLeaseUnavailable(
                    "operation_revision_changed"
                )
            if (
                operation.status is not OrganizationOperationStatus.ORGANIZING
                or operation.lease_token != lease_token
                or operation.lease_expires_at is None
                or _as_utc(operation.lease_expires_at) <= current_time
            ):
                raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
            plan = await session.get(OrganizationPlan, operation.plan_id)
            if plan is None:
                operation.status = OrganizationOperationStatus.UNCERTAIN
                operation.revision = expected_revision + 1
                operation.lease_token = None
                operation.lease_expires_at = None
                operation.error_code = "plan_prerequisites_changed"
                operation.finished_at = current_time
                operation.updated_at = current_time
                await _sync_workflow_stage(
                    session,
                    operation.workflow_id,
                    status=WorkflowStageStatus.UNCERTAIN,
                    child_id=operation.id,
                    reason="organization_uncertain",
                    error_code="plan_prerequisites_changed",
                )
                await session.commit()
                raise OrganizationOperationLeaseUnavailable("plan_revision_changed")
            if plan.revision != operation.plan_revision:
                operation.status = OrganizationOperationStatus.UNCERTAIN
                operation.revision = expected_revision + 1
                operation.lease_token = None
                operation.lease_expires_at = None
                operation.error_code = "plan_prerequisites_changed"
                operation.finished_at = current_time
                operation.updated_at = current_time
                await _sync_workflow_stage(
                    session,
                    operation.workflow_id,
                    status=WorkflowStageStatus.UNCERTAIN,
                    child_id=operation.id,
                    reason="organization_uncertain",
                    error_code="plan_prerequisites_changed",
                )
                await session.commit()
                raise OrganizationOperationLeaseUnavailable("plan_revision_changed")
            plan_revision = (
                select(OrganizationPlan.revision)
                .where(OrganizationPlan.id == OrganizationOperation.plan_id)
                .scalar_subquery()
            )
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.ORGANIZING,
                    OrganizationOperation.lease_token == lease_token,
                    OrganizationOperation.lease_expires_at > current_time,
                    OrganizationOperation.plan_revision == plan_revision,
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
                and error_code in _PLAN_INVALIDATING_ERROR_CODES
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

    async def reconcile_organized(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        source_directory_id: str,
        target_directory_id: str,
        directory_ids: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        """Commit success after a read-only check proved the exact target."""

        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        _validate_identifier(source_directory_id, "invalid_directory_id", maximum=128)
        _validate_identifier(target_directory_id, "invalid_directory_id", maximum=128)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            plan_revision = (
                select(OrganizationPlan.revision)
                .where(OrganizationPlan.id == OrganizationOperation.plan_id)
                .scalar_subquery()
            )
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.UNCERTAIN,
                    OrganizationOperation.plan_revision == plan_revision,
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
                await self._raise_revision_conflict(
                    session, operation_id, expected_revision
                )
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            try:
                normalized_directory_ids = await self._validate_completion_scope(
                    operation_id,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    directory_ids=directory_ids,
                    expected_plan_revision=operation.plan_revision,
                )
                await self._outbox_service.enqueue_directory_dirty(
                    session,
                    operation_id=operation_id,
                    directory_ids=normalized_directory_ids,
                )
                await self._record_history(
                    session,
                    operation,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    completed_at=current_time,
                )
                await _sync_workflow_stage(
                    session,
                    operation.workflow_id,
                    status=WorkflowStageStatus.SUCCEEDED,
                    child_id=operation.id,
                    reason="organization_reconciled",
                )
                await session.commit()
            except _OrganizationCompletionScopeError:
                await session.rollback()
                raise
            except OrganizationOutboxError:
                await session.rollback()
                raise OrganizationOperationConflict(
                    "outbox_persistence_failed"
                ) from None
            except Exception:  # noqa: BLE001 - collapse persistence details
                await session.rollback()
                raise OrganizationOperationConflict(
                    "reconciliation_persistence_failed"
                ) from None
            summary = _summary(operation)
        await self._audit("organize.operation.reconciled", "整理操作已通过远端核对")
        return summary

    async def reconcile_not_applied(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> OrganizationOperationSummary:
        """Mark an uncertain operation retryable only after source reappears."""

        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            plan_revision = (
                select(OrganizationPlan.revision)
                .where(OrganizationPlan.id == OrganizationOperation.plan_id)
                .scalar_subquery()
            )
            result = await session.execute(
                update(OrganizationOperation)
                .where(
                    OrganizationOperation.id == operation_id,
                    OrganizationOperation.revision == expected_revision,
                    OrganizationOperation.status
                    == OrganizationOperationStatus.UNCERTAIN,
                    OrganizationOperation.plan_revision == plan_revision,
                )
                .values(
                    status=OrganizationOperationStatus.FAILED,
                    revision=expected_revision + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code="remote_write_failed",
                    finished_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await self._raise_revision_conflict(
                    session, operation_id, expected_revision
                )
            operation = await session.get(OrganizationOperation, operation_id)
            if operation is None:
                raise OrganizationOperationNotFound
            await _sync_workflow_stage(
                session,
                operation.workflow_id,
                status=WorkflowStageStatus.FAILED,
                child_id=operation.id,
                reason="organization_reconciled_not_applied",
                error_code="remote_write_failed",
            )
            await session.commit()
            summary = _summary(operation)
        await self._audit(
            "organize.operation.reconciled_not_applied",
            "整理操作已核对为未执行，可在确认后重试",
        )
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
                plan_revision = (
                    select(OrganizationPlan.revision)
                    .where(OrganizationPlan.id == OrganizationOperation.plan_id)
                    .scalar_subquery()
                )
                result = await session.execute(
                    update(OrganizationOperation)
                    .where(
                        OrganizationOperation.id == operation_id,
                        OrganizationOperation.revision == expected_revision,
                        OrganizationOperation.status
                        == OrganizationOperationStatus.ORGANIZING,
                        OrganizationOperation.lease_token == lease_token,
                        OrganizationOperation.lease_expires_at > current_time,
                        OrganizationOperation.plan_revision == plan_revision,
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
                    if await self._mark_plan_revision_uncertain(
                        session,
                        operation_id,
                        expected_revision=expected_revision,
                        current_time=current_time,
                    ):
                        raise OrganizationOperationLeaseUnavailable(
                            "plan_revision_changed"
                        )
                    raise OrganizationOperationLeaseUnavailable("lease_is_not_owned")
                operation = await session.get(OrganizationOperation, operation_id)
                if operation is None:
                    raise OrganizationOperationNotFound
                normalized_directory_ids = await self._validate_completion_scope(
                    operation_id,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    directory_ids=directory_ids,
                    expected_plan_revision=operation.plan_revision,
                )
                await self._outbox_service.enqueue_directory_dirty(
                    session,
                    operation_id=operation_id,
                    directory_ids=normalized_directory_ids,
                )
                await self._record_history(
                    session,
                    operation,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    completed_at=current_time,
                )
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
            except _OrganizationCompletionScopeError:
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

    async def _raise_revision_conflict(
        self,
        session: AsyncSession,
        operation_id: str,
        expected_revision: int,
    ) -> None:
        operation = await session.get(OrganizationOperation, operation_id)
        if operation is None:
            raise OrganizationOperationNotFound
        if operation.revision == expected_revision:
            plan = await session.get(OrganizationPlan, operation.plan_id)
            if plan is None or plan.revision != operation.plan_revision:
                raise OrganizationOperationConflict("plan_revision_changed")
        raise OrganizationOperationConflict("operation_revision_changed")

    async def _mark_plan_revision_uncertain(
        self,
        session: AsyncSession,
        operation_id: str,
        *,
        expected_revision: int,
        current_time: datetime,
    ) -> bool:
        """Stop an organizing worker if its frozen plan was changed."""

        operation = await session.get(OrganizationOperation, operation_id)
        if operation is None or operation.revision != expected_revision:
            return False
        plan = await session.get(OrganizationPlan, operation.plan_id)
        if plan is not None and plan.revision == operation.plan_revision:
            return False
        if operation.status is not OrganizationOperationStatus.ORGANIZING:
            return False
        operation.status = OrganizationOperationStatus.UNCERTAIN
        operation.revision = expected_revision + 1
        operation.lease_token = None
        operation.lease_expires_at = None
        operation.error_code = "plan_prerequisites_changed"
        operation.finished_at = current_time
        operation.updated_at = current_time
        await _sync_workflow_stage(
            session,
            operation.workflow_id,
            status=WorkflowStageStatus.UNCERTAIN,
            child_id=operation.id,
            reason="organization_uncertain",
            error_code="plan_prerequisites_changed",
        )
        await session.commit()
        return True

    async def _validate_completion_scope(
        self,
        operation_id: str,
        *,
        source_directory_id: str,
        target_directory_id: str,
        directory_ids: Iterable[str] | None,
        expected_plan_revision: int | None = None,
    ) -> tuple[str, ...]:
        """Keep completion and dirty events inside the immutable plan scope."""

        values = (source_directory_id, target_directory_id)
        if directory_ids is None:
            requested = values
        elif isinstance(directory_ids, (str, bytes)):
            raise _OrganizationCompletionScopeError("invalid_directory_scope")
        else:
            try:
                requested = tuple(directory_ids)
            except TypeError:
                raise _OrganizationCompletionScopeError(
                    "invalid_directory_scope"
                ) from None

        try:
            for directory_id in (*values, *requested):
                _validate_identifier(
                    directory_id, "invalid_directory_id", maximum=128
                )
        except ValueError as error:
            raise _OrganizationCompletionScopeError(str(error)) from None
        if not requested:
            raise _OrganizationCompletionScopeError("invalid_directory_scope")

        plan_scope = await self.plan_execution_scope(
            operation_id,
            expected_plan_revision=expected_plan_revision,
        )
        if plan_scope is None or not set(values).union(requested) <= plan_scope:
            raise _OrganizationCompletionScopeError("directory_scope_unverified")
        return tuple(dict.fromkeys(requested))

    async def _record_history(
        self,
        session: AsyncSession,
        operation: OrganizationOperation,
        *,
        source_directory_id: str,
        target_directory_id: str,
        completed_at: datetime,
    ) -> None:
        """Write completed primary media actions in the same transaction."""

        plan = await session.get(OrganizationPlan, operation.plan_id)
        if plan is None:
            raise OrganizationOperationConflict("plan_not_found")
        try:
            actions = json.loads(plan.actions_json)
            basis = json.loads(plan.basis_json)
        except (TypeError, ValueError):
            raise OrganizationOperationConflict("plan_history_invalid") from None
        if not isinstance(actions, list) or not isinstance(basis, list):
            raise OrganizationOperationConflict("plan_history_invalid")
        basis_by_index = {
            item.get("source_index"): item
            for item in basis
            if isinstance(item, dict) and isinstance(item.get("source_index"), int)
        }
        for action in actions:
            if not isinstance(action, dict) or action.get("kind") != "move":
                continue
            source_object_id = action.get("object_id")
            target_path = action.get("target")
            source_name = action.get("source_name")
            if not all(
                isinstance(value, str) and value
                for value in (source_object_id, target_path, source_name)
            ):
                raise OrganizationOperationConflict("plan_history_invalid")
            existing = await session.scalar(
                select(OrganizationHistoryEntry).where(
                    OrganizationHistoryEntry.operation_id == operation.id,
                    OrganizationHistoryEntry.source_object_id == source_object_id,
                )
            )
            if existing is not None:
                continue
            evidence = basis_by_index.get(action.get("order"), {})
            title = evidence.get("title") if isinstance(evidence, dict) else None
            if not isinstance(title, str) or not title:
                title = source_name
            tmdb_id = evidence.get("tmdb_id") if isinstance(evidence, dict) else None
            if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool):
                tmdb_id = None
            media_type = evidence.get("media_type") if isinstance(evidence, dict) else None
            if media_type not in {"movie", "tv"}:
                media_type = None
            session.add(
                OrganizationHistoryEntry(
                    id="hist_" + uuid.uuid4().hex,
                    operation_id=operation.id,
                    plan_id=operation.plan_id,
                    source_object_id=source_object_id,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    tmdb_id=tmdb_id,
                    title=title,
                    media_type=media_type,
                    source_name=source_name,
                    target_path=target_path,
                    status=OrganizationOperationStatus.ORGANIZED.value,
                    completed_at=completed_at,
                )
            )

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
        source_snapshot_current = False
        if (
            library is not None
            and run is not None
            and plan.source_snapshot_revision is not None
        ):
            source_snapshot_current = await source_snapshot_is_current(
                session,
                library_id=plan.library_id,
                source_scan_run_id=run.id,
                source_snapshot_revision=plan.source_snapshot_revision,
            )
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
            and source_snapshot_current
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
        await audit_event(self._event_logger, event, status)


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


def _existing_operation_summary(
    operation: OrganizationOperation,
    *,
    plan_id: str,
    workflow_id: str | None,
) -> OrganizationOperationSummary:
    if operation.plan_id != plan_id:
        raise OrganizationOperationConflict("idempotency_key_conflict")
    if workflow_id is not None and operation.workflow_id != workflow_id:
        raise OrganizationOperationConflict("workflow_id_conflict")
    return _summary(operation)


async def _plan_has_active_operation(
    session: AsyncSession, plan_id: str
) -> bool:
    """Return whether the plan still carries an unresolved operation.

    A terminal operation (organized / failed / cancelled) releases the plan
    so a fresh operation can be created after a retry; an active one
    (planned / organizing / uncertain) keeps it locked. The status filter
    lives in the query itself so an older terminal row cannot mask a newer
    active one.
    """

    return bool(
        await session.scalar(
            select(
                select(OrganizationOperation.id)
                .where(
                    OrganizationOperation.plan_id == plan_id,
                    OrganizationOperation.status.in_(
                        tuple(_ACTIVE_OPERATION_STATUSES)
                    ),
                )
                .exists()
            )
        )
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
