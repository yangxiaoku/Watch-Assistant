"""Task creation, idempotency, and state transitions."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Resource, Task, TaskState, WorkflowEvidence
from watch_assistant.schemas import (
    EvidenceSource,
    EvidenceStatus,
    RemoteObservation,
    RemoteStatus,
    SubmissionResult,
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
    async def get_status_for_task(
        self,
        remote_ref: str,
        *,
        target_directory_id: str | None,
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
AVAILABILITY_PARENT_MISMATCH = "availability_parent_mismatch"
AVAILABILITY_OBSERVER_TIMEOUT = "availability_observer_timeout"
AVAILABILITY_OBSERVER_UNAVAILABLE = "availability_observer_unavailable"
REMOTE_OBSERVATION_MISSING = "remote_observation_missing"
RECONCILIATION_UNAVAILABLE = "reconciliation_unavailable"
TASK_LEASE_LOST = "lease_claim_lost"


RemoteState = RemoteStatus | RemoteObservation


@dataclass(frozen=True, slots=True, repr=False)
class TaskLease:
    """The immutable claim context used to fence one worker attempt."""

    task_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    resource_id: str | None
    workflow_id: str | None
    target_directory_id: str | None
    action: TaskAction
    encrypted_url_snapshot: str
    encrypted_password_snapshot: str | None
    remote_ref: str | None

    def __repr__(self) -> str:
        return (
            "TaskLease(task_id=<redacted>, lease_owner=<redacted>, "
            f"lease_token=<redacted>, lease_expires_at={self.lease_expires_at!r})"
        )


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
    observation = _scope_observation_to_task(task, observation)
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
            try:
                await sync_child_stage(
                    session,
                    task.workflow_id,
                    WorkflowStageName.PUSH,
                    child_type="task",
                    child_id=task.id,
                    status=workflow_stage_status_for_task_state(state),
                    reason=f"task_{state.value}",
                    error_code=task.error_code,
                    allow_uncertain_resume=(
                        source is EvidenceSource.READONLY_RECONCILIATION
                    ),
                )
            except WorkflowConflict as exc:
                # The workflow was cancelled while the remote operation was in
                # flight, so its stage is already terminal. The task's own
                # terminal outcome and evidence are still recorded; the stage
                # must not be replayed.
                if str(exc) != "workflow_stage_terminal":
                    raise
    return evidence


def recover_after_restart(task: Task, remote_status: RemoteState | None) -> None:
    if _lease_is_live(task, datetime.now(UTC)):
        return
    task.lease_owner = None
    task.lease_token = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)
    if task.state is TaskState.AVAILABLE:
        return
    if remote_status is None:
        task.state = TaskState.UNCERTAIN
        task.error_code = REMOTE_OBSERVATION_MISSING
        task.error_message = _observation_error_message(REMOTE_OBSERVATION_MISSING)
        return
    observation = _as_remote_observation(remote_status)
    if observation is None:
        task.state = TaskState.UNCERTAIN
        task.error_code = REMOTE_OBSERVATION_MISSING
        task.error_message = _observation_error_message(REMOTE_OBSERVATION_MISSING)
        return
    observation = _scope_observation_to_task(task, observation)
    task.state = task_state_from_remote_observation(observation)
    if task.state not in {TaskState.FAILED, TaskState.UNCERTAIN}:
        task.error_code = None
        task.error_message = None
    elif observation.error_code is not None:
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


async def _verified_availability_evidence(
    session: AsyncSession, task: Task
) -> WorkflowEvidence | None:
    """Return only availability evidence owned by this task and workflow."""

    workflow_filter = (
        WorkflowEvidence.workflow_id.is_(None)
        if task.workflow_id is None
        else WorkflowEvidence.workflow_id == task.workflow_id
    )
    return await session.scalar(
        select(WorkflowEvidence)
        .where(
            WorkflowEvidence.task_id == task.id,
            workflow_filter,
            WorkflowEvidence.stage == WorkflowStageName.AVAILABILITY,
            WorkflowEvidence.evidence_type == "availability_receipt",
            WorkflowEvidence.source == EvidenceSource.READONLY_RECONCILIATION.value,
            WorkflowEvidence.subject_id == task.id,
            WorkflowEvidence.status == EvidenceStatus.AVAILABLE.value,
            WorkflowEvidence.verified.is_(True),
        )
        .order_by(WorkflowEvidence.observed_at.desc(), WorkflowEvidence.id.desc())
    )


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
    task.lease_token = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)


def _task_lease(
    task: Task,
    owner: str,
    token: str,
    expires_at: datetime,
) -> TaskLease:
    return TaskLease(
        task_id=task.id,
        lease_owner=owner,
        lease_token=token,
        lease_expires_at=expires_at,
        resource_id=task.resource_id,
        workflow_id=task.workflow_id,
        target_directory_id=task.target_directory_id,
        action=task.action,
        encrypted_url_snapshot=task.encrypted_url_snapshot,
        encrypted_password_snapshot=task.encrypted_password_snapshot,
        remote_ref=task.remote_ref,
    )


async def _fenced_task(
    session: AsyncSession,
    lease: TaskLease,
    current_time: datetime,
    *,
    require_live_lease: bool = True,
) -> Task | None:
    """Acquire the task row for a final write using the lease predicates."""

    predicates = [
        Task.id == lease.task_id,
        Task.state == TaskState.SUBMITTING,
        Task.lease_owner == lease.lease_owner,
        Task.lease_token == lease.lease_token,
        Task.lease_expires_at.is_not(None),
    ]
    if require_live_lease:
        predicates.append(Task.lease_expires_at > current_time)
    result = await session.execute(
        update(Task)
        .where(*predicates)
        .values(updated_at=current_time)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    return await session.get(Task, lease.task_id)


async def _fence_task_commit(
    session: AsyncSession,
    lease: TaskLease,
    current_time: datetime,
    *,
    require_live_lease: bool = True,
    expected_expires_at: datetime | None = None,
) -> bool:
    """Prove the claim still owns the transaction immediately before commit."""

    predicates = [
        Task.id == lease.task_id,
        Task.lease_owner == lease.lease_owner,
        Task.lease_token == lease.lease_token,
        Task.lease_expires_at.is_not(None),
    ]
    observed_expires_at = None
    if require_live_lease:
        if expected_expires_at is None:
            return False
        observed_expires_at = _as_utc(expected_expires_at)
        current_time = max(_as_utc(current_time), datetime.now(UTC))
        predicates.append(Task.lease_expires_at > current_time)
    result = await session.execute(
        update(Task)
        .where(*predicates)
        .values(updated_at=current_time)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    if (
        require_live_lease
        and observed_expires_at is not None
        and observed_expires_at <= datetime.now(UTC)
    ):
        # The final conditional update can itself wait behind SQLite's write
        # lock. Roll back if the observed lease expired before that write ran.
        await session.rollback()
        return False
    return True


def _release_task_lease(task: Task) -> None:
    task.lease_owner = None
    task.lease_token = None
    task.lease_expires_at = None


def _validate_lease_parameters(owner: str, lease_duration: timedelta) -> None:
    if not isinstance(owner, str) or not owner or len(owner) > 100:
        raise ValueError("invalid_worker_owner")
    if lease_duration.total_seconds() <= 0:
        raise ValueError("invalid_lease_duration")


def _lease_is_live(task: Task, current_time: datetime) -> bool:
    return bool(
        task.lease_owner
        and task.lease_token
        and task.lease_expires_at is not None
        and _as_utc(task.lease_expires_at) > current_time
    )


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
                    if (
                        workflow_id is not None
                        and existing.workflow_id not in {None, workflow_id}
                    ):
                        raise WorkflowConflict("workflow_conflict")
                    availability_evidence = None
                    if existing.state is TaskState.AVAILABLE:
                        availability_evidence = await _verified_availability_evidence(
                            session, existing
                        )
                        if availability_evidence is None:
                            raise WorkflowConflict("workflow_evidence_required")
                    if workflow_id is not None and existing.workflow_id is None:
                        existing.workflow_id = workflow_id
                        task_evidence = list(
                            await session.scalars(
                                select(WorkflowEvidence).where(
                                    WorkflowEvidence.task_id == existing.id
                                )
                            )
                        )
                        for evidence in task_evidence:
                            if evidence.workflow_id is None:
                                evidence.workflow_id = workflow_id
                            elif evidence.workflow_id != workflow_id:
                                raise WorkflowConflict("workflow_conflict")
                        await link_child(
                            session,
                            workflow_id,
                            WorkflowStageName.PUSH,
                            "task",
                            existing.id,
                        )
                        if (
                            existing.state is TaskState.AVAILABLE
                            and availability_evidence is not None
                        ):
                            await advance_availability_from_evidence(
                                session,
                                workflow_id,
                                existing.id,
                                availability_evidence,
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

    async def claim_next(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> TaskLease | None:
        """Conditionally claim the oldest queued task in SQLite.

        The candidate read is deliberately followed by a conditional update.
        Concurrent workers may read the same candidate, but only the update
        whose state and lease predicates still match can commit the claim.
        """

        _validate_lease_parameters(owner, lease_duration)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            while True:
                task = await session.scalar(
                    select(Task)
                    .where(
                        Task.state == TaskState.QUEUED,
                        Task.lease_owner.is_(None),
                        Task.lease_token.is_(None),
                        (
                            Task.lease_expires_at.is_(None)
                            | (Task.lease_expires_at <= current_time)
                        ),
                    )
                    .order_by(Task.created_at.asc(), Task.id.asc())
                    .limit(1)
                )
                if task is None:
                    return None
                token = uuid4().hex
                expires_at = current_time + lease_duration
                result = await session.execute(
                    update(Task)
                    .where(
                        Task.id == task.id,
                        Task.state == TaskState.QUEUED,
                        Task.lease_owner.is_(None),
                        Task.lease_token.is_(None),
                        (
                            Task.lease_expires_at.is_(None)
                            | (Task.lease_expires_at <= current_time)
                        ),
                    )
                    .values(
                        state=TaskState.SUBMITTING,
                        attempts=Task.attempts + 1,
                        lease_owner=owner,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        updated_at=current_time,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return _task_lease(task, owner, token, expires_at)

    async def claim_expired(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> TaskLease | None:
        """Take over one expired submitting task with a new fencing token."""

        _validate_lease_parameters(owner, lease_duration)
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            while True:
                task = await session.scalar(
                    select(Task)
                    .where(
                        Task.state == TaskState.SUBMITTING,
                        Task.lease_expires_at.is_not(None),
                        Task.lease_expires_at <= current_time,
                    )
                    .order_by(Task.updated_at.asc(), Task.id.asc())
                    .limit(1)
                )
                if task is None:
                    return None
                token = uuid4().hex
                expires_at = current_time + lease_duration
                result = await session.execute(
                    update(Task)
                    .where(
                        Task.id == task.id,
                        Task.state == TaskState.SUBMITTING,
                        Task.lease_expires_at.is_not(None),
                        Task.lease_expires_at <= current_time,
                    )
                    .values(
                        lease_owner=owner,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        updated_at=current_time,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return _task_lease(task, owner, token, expires_at)

    async def is_lease_active(
        self, lease: TaskLease, *, now: datetime | None = None
    ) -> bool:
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            lease_expires_at = await session.scalar(
                select(Task.lease_expires_at).where(
                    Task.id == lease.task_id,
                    Task.state == TaskState.SUBMITTING,
                    Task.lease_owner == lease.lease_owner,
                    Task.lease_token == lease.lease_token,
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at > current_time,
                )
            )
            if lease_expires_at is None:
                return False
            # The SQL predicate uses the time captured before the query. A
            # slow SQLite read must not turn an already expired claim into a
            # positive liveness result.
            check_time = current_time if now is not None else datetime.now(UTC)
            return _as_utc(lease_expires_at) > check_time

    async def renew(
        self,
        lease: TaskLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """Renew only the still-live claim that owns this exact token."""

        _validate_lease_parameters(lease.lease_owner, lease_duration)
        async with self._session_factory() as session:
            current_time = _as_utc(now or datetime.now(UTC))
            observed_expires_at = await session.scalar(
                select(Task.lease_expires_at).where(
                    Task.id == lease.task_id,
                    Task.state == TaskState.SUBMITTING,
                    Task.lease_owner == lease.lease_owner,
                    Task.lease_token == lease.lease_token,
                    Task.lease_expires_at.is_not(None),
                )
            )
            if observed_expires_at is None:
                return False
            observed_expires_at = _as_utc(observed_expires_at)
            if now is None:
                current_time = datetime.now(UTC)
            if observed_expires_at <= current_time:
                return False
            expires_at = current_time + lease_duration
            result = await session.execute(
                update(Task)
                .where(
                    Task.id == lease.task_id,
                    Task.state == TaskState.SUBMITTING,
                    Task.lease_owner == lease.lease_owner,
                    Task.lease_token == lease.lease_token,
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at > current_time,
                )
                .values(lease_expires_at=expires_at, updated_at=current_time)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            if now is None and observed_expires_at <= datetime.now(UTC):
                # The conditional update can be delayed behind SQLite's write
                # lock. Do not commit a renewal that crossed the old expiry.
                await session.rollback()
                return False
            await session.commit()
            return True

    async def finish_submission(
        self, lease: TaskLease, result: SubmissionResult
    ) -> Task | None:
        """Persist a submission result only while the claim is still fenced."""

        current_time = datetime.now(UTC)
        async with self._session_factory() as session:
            task = await _fenced_task(session, lease, current_time)
            if task is None:
                return None
            task.remote_ref = result.remote_ref
            task.error_code = result.error_code
            task.error_message = result.error_message
            task.submitted_at = (
                current_time
                if result.status
                in {
                    RemoteStatus.ACCEPTED,
                    RemoteStatus.SUBMITTED,
                    RemoteStatus.DOWNLOADING,
                    RemoteStatus.AVAILABLE,
                }
                else None
            )
            await apply_remote_status(
                session,
                task,
                result.status,
                source=EvidenceSource.SUBMISSION_RECEIPT,
                verified_available=False,
            )
            commit_time = datetime.now(UTC)
            if not await _fence_task_commit(
                session,
                lease,
                commit_time,
                expected_expires_at=task.lease_expires_at,
            ):
                return None
            _release_task_lease(task)
            task.updated_at = commit_time
            await session.commit()
            return task

    async def finish_recovery(
        self,
        lease: TaskLease,
        remote_status: RemoteState | None,
        *,
        allow_expired: bool = False,
    ) -> Task | None:
        """Persist a read-only recovery observation under the exact claim fence.

        ``allow_expired`` is reserved for marking a lost old claim.  The
        owner/token predicates still prevent a later worker from being changed.
        """

        current_time = datetime.now(UTC)
        observation = _as_remote_observation(remote_status)
        if observation is None:
            observation = RemoteObservation(
                status=RemoteStatus.UNCERTAIN,
                error_code=REMOTE_OBSERVATION_MISSING,
            )
        async with self._session_factory() as session:
            task = await _fenced_task(
                session,
                lease,
                current_time,
                require_live_lease=not allow_expired,
            )
            if task is None:
                return None
            observation = _scope_observation_to_task(task, observation)
            await apply_remote_status(
                session,
                task,
                observation,
                source=EvidenceSource.READONLY_RECONCILIATION,
            )
            commit_time = datetime.now(UTC)
            if not await _fence_task_commit(
                session,
                lease,
                commit_time,
                require_live_lease=not allow_expired,
                expected_expires_at=task.lease_expires_at,
            ):
                return None
            _release_task_lease(task)
            task.updated_at = commit_time
            await session.commit()
            return task

    async def mark_lease_lost(self, lease: TaskLease) -> Task | None:
        """Fence a worker that lost its claim into an explicit review state."""

        return await self.finish_recovery(
            lease,
            RemoteObservation(
                status=RemoteStatus.UNCERTAIN,
                error_code=TASK_LEASE_LOST,
            ),
            allow_expired=True,
        )

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
            if task.state is TaskState.SUBMITTING and (
                task.lease_owner is not None or task.lease_token is not None
            ):
                raise TaskNotReconcilable("task_lease_active")
            remote_ref = task.remote_ref
            target_directory_id = task.target_directory_id
            lease_owner = task.lease_owner
            lease_token = task.lease_token
        try:
            remote_status = await read_task_status(
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
            if (
                task.remote_ref != remote_ref
                or task.lease_owner != lease_owner
                or task.lease_token != lease_token
            ):
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

    async def retry_failed_batch(
        self,
        *,
        limit: int = 50,
        allowed_actions: frozenset[TaskAction] | None = None,
    ) -> dict[str, object]:
        """Retry recently failed tasks without touching uncertain ones."""
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(Task.state == TaskState.FAILED)
                        .order_by(Task.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            )
        retried = 0
        failures: list[dict[str, str]] = []
        for task in rows:
            try:
                await self.retry(task.id, allowed_actions=allowed_actions)
                retried += 1
            except Exception as exc:  # noqa: BLE001 - one task must not stop the batch
                code = str(exc)
                if code not in {"uncertain_requires_verification"}:
                    code = "task_not_retryable"
                failures.append({"task_id": task.id, "code": code})
        return {
            "requested": len(rows),
            "retried": retried,
            "failed": failures,
        }

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
                    or_(
                        Task.state == TaskState.QUEUED,
                        # An uncertain task with no remote identity cannot be
                        # reconciled; allow abandoning it explicitly instead of
                        # leaving it stuck forever.
                        and_(
                            Task.state == TaskState.UNCERTAIN,
                            Task.remote_ref.is_(None),
                        ),
                    ),
                    Task.lease_owner.is_(None),
                    Task.lease_token.is_(None),
                )
                .values(
                    state=TaskState.CANCELLED,
                    error_code="cancelled",
                    error_message="task_cancelled",
                    lease_token=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise InvalidCancelState("task is not cancellable")
            task.state = TaskState.CANCELLED
            task.error_code = "cancelled"
            task.error_message = "task_cancelled"
            task.lease_token = None
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


async def read_task_status(
    adapter: TaskStatusAdapter,
    remote_ref: str,
    *,
    target_directory_id: str | None,
) -> RemoteState | None:
    """Read status through the target-aware, read-only observation contract."""

    target_aware = getattr(adapter, "get_status_for_task", None)
    if not callable(target_aware):
        raise ReconciliationUnavailable(RECONCILIATION_UNAVAILABLE)
    return await target_aware(remote_ref, target_directory_id=target_directory_id)


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
        AVAILABILITY_PARENT_MISMATCH: "远端文件父目录核验不一致。",
        AVAILABILITY_OBSERVER_TIMEOUT: "远端文件只读核验超时。",
        AVAILABILITY_OBSERVER_UNAVAILABLE: "远端文件只读观察器暂不可用。",
        REMOTE_OBSERVATION_MISSING: "远端只读核对没有返回完整观察结果。",
        RECONCILIATION_UNAVAILABLE: "远端只读核对暂时不可用。",
        TASK_LEASE_LOST: "任务执行权已变化，外部结果待确认，未继续提交。",
    }.get(error_code, "远端结果待确认，系统未重复提交。")


def _scope_observation_to_task(
    task: Task, observation: RemoteObservation
) -> RemoteObservation:
    """Reject a complete observation that proves a different target directory."""

    if (
        not observation.availability_verified
        or task.target_directory_id is None
        or _stable_remote_id(task.target_directory_id)
        == _stable_remote_id(observation.parent_id)
    ):
        return observation
    return RemoteObservation(
        status=RemoteStatus.UNCERTAIN,
        error_code=AVAILABILITY_PARENT_MISMATCH,
    )


def _stable_remote_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    if not normalized.isdigit() or normalized.startswith("0"):
        return None
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
