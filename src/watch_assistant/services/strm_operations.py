"""Durable local ledger for STRM synchronization operations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import (
    StrmOperation,
    StrmOperationKind,
    StrmOperationStatus,
)


class StrmOperationError(ValueError):
    """Stable local STRM operation contract error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StrmOperationNotFound(LookupError):
    """Raised when a requested STRM operation is not in the local ledger."""


SessionFence = Callable[[AsyncSession], Awaitable[bool]]


_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 256
_MUTATING_KINDS = (
    StrmOperationKind.FULL,
    StrmOperationKind.INCREMENTAL,
    StrmOperationKind.CLEANUP,
)
_CLAIM_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class StrmOperationSummary:
    operation_id: str
    library_id: str
    source_scan_run_id: str
    kind: str
    status: str
    workflow_id: str | None
    generated: int
    unchanged: int
    skipped: int
    failed: int
    retired: int
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StrmOperationService:
    """Persist operation lifecycle independently from manifest file writes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        library_id: str,
        source_scan_run_id: str,
        kind: StrmOperationKind | str,
        workflow_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> StrmOperationSummary:
        _validate_identifier(library_id, "library_id", maximum=128)
        _validate_identifier(source_scan_run_id, "source_scan_run_id", maximum=64)
        if workflow_id is not None:
            _validate_identifier(workflow_id, "workflow_id", maximum=40)
        try:
            operation_kind = StrmOperationKind(kind)
        except ValueError:
            raise StrmOperationError("invalid_operation_kind") from None
        if idempotency_key is not None:
            _validate_idempotency_key(idempotency_key)
        operation_id = (
            _idempotent_operation_id(library_id, operation_kind, idempotency_key)
            if idempotency_key is not None
            else "strm_op_" + uuid4().hex
        )
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            existing = await session.get(StrmOperation, operation_id)
            if existing is not None:
                _ensure_same_request(
                    existing,
                    library_id=library_id,
                    source_scan_run_id=source_scan_run_id,
                    kind=operation_kind,
                    workflow_id=workflow_id,
                )
                return _summary(existing)
            operation = StrmOperation(
                id=operation_id,
                library_id=library_id,
                source_scan_run_id=source_scan_run_id,
                kind=operation_kind,
                workflow_id=workflow_id,
                status=StrmOperationStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                existing = await session.get(StrmOperation, operation_id)
                if existing is None:
                    raise StrmOperationError("strm_operation_creation_conflict") from error
                _ensure_same_request(
                    existing,
                    library_id=library_id,
                    source_scan_run_id=source_scan_run_id,
                    kind=operation_kind,
                    workflow_id=workflow_id,
                )
                return _summary(existing)
            await session.refresh(operation)
            return _summary(operation)

    async def start(
        self,
        operation_id: str,
        *,
        lease_owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> StrmOperationSummary:
        summary, _ = await self.claim_start(
            operation_id,
            lease_owner=lease_owner,
            lease_duration=lease_duration,
            now=now,
        )
        return summary

    async def claim_start(
        self,
        operation_id: str,
        *,
        lease_owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> tuple[StrmOperationSummary, bool]:
        """Atomically claim a queued operation for one executor."""

        duration = _validate_lease_duration(lease_duration)
        _validate_optional_owner(lease_owner)
        current_time = _as_utc(now) or datetime.now(UTC)
        _validate_identifier(operation_id, "operation_id", maximum=64)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            library_id = current.library_id

        # Serialize claims for the actual manifest scope. The conditional SQL
        # update below remains the durable cross-process guard; this lock only
        # closes the common same-process read/claim race.
        async with _claim_lock(self._session_factory, library_id):
            for attempt in range(3):
                try:
                    async with self._session_factory() as session:
                        current = await session.get(StrmOperation, operation_id)
                        if current is None:
                            raise StrmOperationNotFound(operation_id)
                        if current.status is not StrmOperationStatus.QUEUED:
                            return _summary(current), False
                        active_exists = exists(
                            select(StrmOperation.id).where(
                                StrmOperation.library_id == current.library_id,
                                StrmOperation.status == StrmOperationStatus.RUNNING,
                                StrmOperation.kind.in_(_MUTATING_KINDS),
                                StrmOperation.id != operation_id,
                            )
                        )
                        token = uuid4().hex
                        result = await session.execute(
                            update(StrmOperation)
                            .where(
                                StrmOperation.id == operation_id,
                                StrmOperation.status == StrmOperationStatus.QUEUED,
                                ~active_exists,
                            )
                            .execution_options(synchronize_session=False)
                            .values(
                                status=StrmOperationStatus.RUNNING,
                                started_at=current_time,
                                updated_at=current_time,
                                heartbeat_at=current_time,
                                lease_expires_at=current_time + duration,
                                lease_owner=token,
                            )
                        )
                        if result.rowcount != 1:
                            await session.rollback()
                            current = await session.get(StrmOperation, operation_id)
                            if current is None:
                                raise StrmOperationNotFound(operation_id)
                            return _summary(current), False
                        await session.commit()
                        await session.refresh(current)
                        return _summary(current), True
                except OperationalError as error:
                    if attempt == 2 or not _is_database_lock(error):
                        raise
                    await asyncio.sleep(0.02 * (attempt + 1))
            raise StrmOperationError("strm_operation_claim_conflict")

    async def heartbeat(
        self,
        operation_id: str,
        *,
        lease_owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> StrmOperationSummary:
        duration = _validate_lease_duration(lease_duration)
        _validate_optional_owner(lease_owner)
        current_time = _as_utc(now) or datetime.now(UTC)
        _validate_identifier(operation_id, "operation_id", maximum=64)
        if lease_owner is None:
            raise StrmOperationError("strm_operation_lease_required")
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status is not StrmOperationStatus.RUNNING:
                return _summary(current)
            predicates = [
                StrmOperation.id == operation_id,
                StrmOperation.status == StrmOperationStatus.RUNNING,
                StrmOperation.lease_owner == lease_owner,
                StrmOperation.lease_expires_at.is_not(None),
                StrmOperation.lease_expires_at > current_time,
            ]
            result = await session.execute(
                update(StrmOperation)
                .where(*predicates)
                .execution_options(synchronize_session=False)
                .values(
                    heartbeat_at=current_time,
                    lease_expires_at=current_time + duration,
                    updated_at=current_time,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(StrmOperation, operation_id)
                if current is None:
                    raise StrmOperationNotFound(operation_id)
                if current.status is not StrmOperationStatus.RUNNING:
                    return _summary(current)
                raise StrmOperationError("strm_operation_lease_lost")
            await session.commit()
            await session.refresh(current)
            return _summary(current)

    async def progress(
        self,
        operation_id: str,
        *,
        generated: int,
        unchanged: int,
        skipped: int,
        failed: int,
        retired: int,
        lease_owner: str | None = None,
    ) -> StrmOperationSummary:
        """Persist a checkpoint while a local operation is still running."""

        _validate_counts(generated, unchanged, skipped, failed, retired)
        _validate_identifier(operation_id, "operation_id", maximum=64)
        _validate_optional_owner(lease_owner)
        if lease_owner is None:
            raise StrmOperationError("strm_operation_lease_required")
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status is not StrmOperationStatus.RUNNING:
                return _summary(current)
            result = await session.execute(
                update(StrmOperation)
                .where(
                    StrmOperation.id == operation_id,
                    StrmOperation.status == StrmOperationStatus.RUNNING,
                    StrmOperation.lease_owner == lease_owner,
                    StrmOperation.lease_expires_at.is_not(None),
                    StrmOperation.lease_expires_at > now,
                )
                .execution_options(synchronize_session=False)
                .values(
                    generated=generated,
                    unchanged=unchanged,
                    skipped=skipped,
                    failed=failed,
                    retired=retired,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(StrmOperation, operation_id)
                if current is None:
                    raise StrmOperationNotFound(operation_id)
                if current.status is not StrmOperationStatus.RUNNING:
                    return _summary(current)
                raise StrmOperationError("strm_operation_lease_lost")
            await session.commit()
            await session.refresh(current)
            return _summary(current)

    async def complete(
        self,
        operation_id: str,
        *,
        generated: int,
        unchanged: int,
        skipped: int,
        failed: int,
        retired: int,
        lease_owner: str | None = None,
        durable_fence: SessionFence | None = None,
    ) -> StrmOperationSummary:
        _validate_counts(generated, unchanged, skipped, failed, retired)
        return await self._finish(
            operation_id,
            status=StrmOperationStatus.SUCCEEDED,
            lease_owner=lease_owner,
            generated=generated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            retired=retired,
            durable_fence=durable_fence,
        )

    async def fail(
        self,
        operation_id: str,
        *,
        error_code: str,
        generated: int = 0,
        unchanged: int = 0,
        skipped: int = 0,
        failed: int = 0,
        retired: int = 0,
        lease_owner: str | None = None,
        durable_fence: SessionFence | None = None,
    ) -> StrmOperationSummary:
        _validate_counts(generated, unchanged, skipped, failed, retired)
        if not isinstance(error_code, str) or not error_code or len(error_code) > 100:
            raise StrmOperationError("invalid_error_code")
        return await self._finish(
            operation_id,
            status=StrmOperationStatus.FAILED,
            error_code=error_code,
            lease_owner=lease_owner,
            generated=generated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            retired=retired,
            durable_fence=durable_fence,
        )

    async def cancel(
        self,
        operation_id: str,
        *,
        error_code: str = "strm_operation_cancelled",
        lease_owner: str | None = None,
    ) -> StrmOperationSummary:
        _validate_error_code(error_code)
        _validate_identifier(operation_id, "operation_id", maximum=64)
        _validate_optional_owner(lease_owner)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status in {
                StrmOperationStatus.SUCCEEDED,
                StrmOperationStatus.FAILED,
                StrmOperationStatus.TIMEOUT,
                StrmOperationStatus.CANCELLED,
            }:
                return _summary(current)
            predicates = [
                StrmOperation.id == operation_id,
                StrmOperation.status.in_(
                    (StrmOperationStatus.QUEUED, StrmOperationStatus.RUNNING)
                ),
            ]
            if current.status is StrmOperationStatus.RUNNING:
                if lease_owner is not None:
                    predicates.extend(
                        (
                            StrmOperation.lease_owner == lease_owner,
                            StrmOperation.lease_expires_at.is_not(None),
                            StrmOperation.lease_expires_at > now,
                        )
                    )
                elif current.lease_owner is None:
                    raise StrmOperationError("strm_operation_lease_lost")
            result = await session.execute(
                update(StrmOperation)
                .where(*predicates)
                .execution_options(synchronize_session=False)
                .values(
                    status=StrmOperationStatus.CANCELLED,
                    error_code=error_code,
                    started_at=current.started_at or now,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(StrmOperation, operation_id)
                if current is None:
                    raise StrmOperationNotFound(operation_id)
                if current.status in {
                    StrmOperationStatus.SUCCEEDED,
                    StrmOperationStatus.FAILED,
                    StrmOperationStatus.TIMEOUT,
                    StrmOperationStatus.CANCELLED,
                }:
                    return _summary(current)
                raise StrmOperationError("strm_operation_lease_lost")
            await session.commit()
            await session.refresh(current)
            return _summary(current)

    async def resume(self, operation_id: str) -> StrmOperationSummary:
        """Requeue a terminal local operation for an explicit retry.

        The manifest reconciler is idempotent, so retrying after a process
        interruption is safe. This method only changes the durable state; a
        caller still has to start the operation through the normal execution
        path.
        """

        _validate_identifier(operation_id, "operation_id", maximum=64)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status not in {
                StrmOperationStatus.FAILED,
                StrmOperationStatus.TIMEOUT,
                StrmOperationStatus.CANCELLED,
            }:
                return _summary(current)
            now = datetime.now(UTC)
            result = await session.execute(
                update(StrmOperation)
                .where(
                    StrmOperation.id == operation_id,
                    StrmOperation.status.in_(
                        (
                            StrmOperationStatus.FAILED,
                            StrmOperationStatus.TIMEOUT,
                            StrmOperationStatus.CANCELLED,
                        )
                    ),
                )
                .execution_options(synchronize_session=False)
                .values(
                    status=StrmOperationStatus.QUEUED,
                    error_code=None,
                    generated=0,
                    unchanged=0,
                    skipped=0,
                    failed=0,
                    retired=0,
                    started_at=None,
                    finished_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(StrmOperation, operation_id)
                if current is None:
                    raise StrmOperationNotFound(operation_id)
                return _summary(current)
            await session.commit()
            await session.refresh(current)
            return _summary(current)

    async def reconcile_cleanup_applied(
        self, operation_id: str, *, retired: int
    ) -> StrmOperationSummary:
        """Close a failed cleanup ledger row after its plan is proven applied.

        The caller must establish the applied-plan proof separately. This method
        only performs a compare-and-set over terminal cleanup failures, so an
        active or newly claimed executor cannot be overwritten.
        """

        _validate_counts(0, 0, 0, 0, retired)
        _validate_identifier(operation_id, "operation_id", maximum=64)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status is StrmOperationStatus.SUCCEEDED:
                return _summary(current)
            if current.kind is not StrmOperationKind.CLEANUP or current.status not in {
                StrmOperationStatus.FAILED,
                StrmOperationStatus.TIMEOUT,
                StrmOperationStatus.CANCELLED,
            }:
                raise StrmOperationError("strm_operation_not_reconcilable")
            result = await session.execute(
                update(StrmOperation)
                .where(
                    StrmOperation.id == operation_id,
                    StrmOperation.kind == StrmOperationKind.CLEANUP,
                    StrmOperation.status.in_(
                        (
                            StrmOperationStatus.FAILED,
                            StrmOperationStatus.TIMEOUT,
                            StrmOperationStatus.CANCELLED,
                        )
                    ),
                )
                .execution_options(synchronize_session=False)
                .values(
                    status=StrmOperationStatus.SUCCEEDED,
                    generated=0,
                    unchanged=0,
                    skipped=0,
                    failed=0,
                    retired=retired,
                    error_code=None,
                    started_at=current.started_at or now,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                observed = await session.get(StrmOperation, operation_id)
                if observed is None:
                    raise StrmOperationNotFound(operation_id)
                if observed.status is StrmOperationStatus.SUCCEEDED:
                    return _summary(observed)
                raise StrmOperationError("strm_operation_not_reconcilable")
            try:
                await session.commit()
            except Exception:
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001, S110 - preserve the commit error
                    pass
                observed = await self._load_terminal(operation_id)
                if observed is not None:
                    return observed
                raise
            try:
                await session.refresh(current)
            except Exception:
                observed = await self._load_terminal(operation_id)
                if observed is not None:
                    return observed
                raise
            return _summary(current)

    async def is_cancelled(self, operation_id: str) -> bool:
        """Read the cancellation bit without exposing persisted credentials."""

        operation = await self._load(operation_id)
        return operation.status is StrmOperationStatus.CANCELLED

    async def is_lease_active(
        self,
        operation_id: str,
        *,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> bool:
        _validate_identifier(operation_id, "operation_id", maximum=64)
        _validate_optional_owner(lease_owner)
        if lease_owner is None:
            return False
        current_time = _as_utc(now) or datetime.now(UTC)
        async with self._session_factory() as session:
            operation = await session.get(StrmOperation, operation_id)
            return bool(
                operation is not None
                and operation.status is StrmOperationStatus.RUNNING
                and operation.lease_owner == lease_owner
                and operation.lease_expires_at is not None
                and _as_utc(operation.lease_expires_at) > current_time
            )

    async def get_lease_token(self, operation_id: str) -> str | None:
        """Read the internal fencing token without putting it in a summary."""

        operation = await self._load(operation_id)
        return operation.lease_owner

    async def get(self, operation_id: str) -> StrmOperationSummary:
        return _summary(await self._load(operation_id))

    async def recover_incomplete(
        self,
        *,
        now: datetime | None = None,
        error_code: str = "strm_operation_recovered",
    ) -> int:
        """Recover operations without taking a live lease from another process."""

        _validate_error_code(error_code)
        current_time = _as_utc(now) or datetime.now(UTC)
        return await self._recover(
            current_time=current_time,
            error_code=error_code,
            cutoff=None,
            respect_lease=True,
        )

    async def recover_stale(
        self,
        *,
        max_age: timedelta,
        now: datetime | None = None,
        error_code: str = "strm_operation_timeout",
    ) -> int:
        """Terminalize queued/running operations older than the timeout."""

        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise StrmOperationError("invalid_operation_timeout")
        _validate_error_code(error_code)
        current_time = _as_utc(now) or datetime.now(UTC)
        return await self._recover(
            current_time=current_time,
            error_code=error_code,
            cutoff=current_time - max_age,
            respect_lease=True,
        )

    async def list(
        self, library_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[list[StrmOperationSummary], str | None]:
        _validate_identifier(library_id, "library_id", maximum=128)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise StrmOperationError("invalid_limit")
        cursor_key = _decode_cursor(cursor)
        query = (
            select(StrmOperation)
            .where(StrmOperation.library_id == library_id)
            .order_by(
                StrmOperation.created_at.desc(),
                StrmOperation.id.desc(),
            )
            .limit(limit + 1)
        )
        if cursor_key is not None:
            created_at, operation_id = cursor_key
            query = query.where(
                or_(
                    StrmOperation.created_at < created_at,
                    and_(
                        StrmOperation.created_at == created_at,
                        StrmOperation.id < operation_id,
                    ),
                )
            )
        async with self._session_factory() as session:
            rows = list((await session.scalars(query)).all())
        has_more = len(rows) > limit
        summaries = [_summary(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(summaries[-1]) if has_more else None
        return summaries, next_cursor

    async def _finish(
        self,
        operation_id: str,
        *,
        status: StrmOperationStatus,
        lease_owner: str | None,
        error_code: str | None = None,
        generated: int,
        unchanged: int,
        skipped: int,
        failed: int,
        retired: int,
        durable_fence: SessionFence | None,
    ) -> StrmOperationSummary:
        _validate_identifier(operation_id, "operation_id", maximum=64)
        _validate_optional_owner(lease_owner)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation_id)
            if current is None:
                raise StrmOperationNotFound(operation_id)
            if current.status in {
                StrmOperationStatus.SUCCEEDED,
                StrmOperationStatus.FAILED,
                StrmOperationStatus.TIMEOUT,
                StrmOperationStatus.CANCELLED,
            }:
                return _summary(current)

            predicates = [StrmOperation.id == operation_id]
            if current.status is StrmOperationStatus.QUEUED:
                if status is not StrmOperationStatus.FAILED or lease_owner is not None:
                    raise StrmOperationError("strm_operation_not_running")
                predicates.append(StrmOperation.status == StrmOperationStatus.QUEUED)
            elif current.status is StrmOperationStatus.RUNNING:
                if lease_owner is None:
                    raise StrmOperationError("strm_operation_lease_required")
                predicates.extend(
                    (
                        StrmOperation.status == StrmOperationStatus.RUNNING,
                        StrmOperation.lease_owner == lease_owner,
                        StrmOperation.lease_expires_at.is_not(None),
                        StrmOperation.lease_expires_at > now,
                    )
                )
            else:
                raise StrmOperationError("strm_operation_not_running")

            if durable_fence is not None and not await durable_fence(session):
                await session.rollback()
                raise StrmOperationError("strm_operation_lease_lost")

            result = await session.execute(
                update(StrmOperation)
                .where(*predicates)
                .execution_options(synchronize_session=False)
                .values(
                    status=status,
                    generated=generated,
                    unchanged=unchanged,
                    skipped=skipped,
                    failed=failed,
                    retired=retired,
                    error_code=error_code,
                    started_at=current.started_at or now,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(StrmOperation, operation_id)
                if current is None:
                    raise StrmOperationNotFound(operation_id)
                if current.status in {
                    StrmOperationStatus.SUCCEEDED,
                    StrmOperationStatus.FAILED,
                    StrmOperationStatus.TIMEOUT,
                    StrmOperationStatus.CANCELLED,
                }:
                    return _summary(current)
                raise StrmOperationError("strm_operation_lease_lost")
            try:
                await session.commit()
            except Exception:
                # The database may have committed before the acknowledgement
                # failed. Observe a fresh row before allowing the caller to
                # mark a durable manifest result as failed.
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001, S110 - preserve the commit error
                    pass
                observed = await self._load_terminal(operation_id)
                if observed is not None:
                    return observed
                raise
            try:
                await session.refresh(current)
            except Exception:
                observed = await self._load_terminal(operation_id)
                if observed is not None:
                    return observed
                raise
            return _summary(current)

    async def _load(self, operation_id: str) -> StrmOperation:
        _validate_identifier(operation_id, "operation_id", maximum=64)
        async with self._session_factory() as session:
            operation = await session.get(StrmOperation, operation_id)
            if operation is None:
                raise StrmOperationNotFound(operation_id)
            return operation

    async def _load_terminal(self, operation_id: str) -> StrmOperationSummary | None:
        async with self._session_factory() as session:
            operation = await session.get(StrmOperation, operation_id)
            if operation is None or operation.status not in {
                StrmOperationStatus.SUCCEEDED,
                StrmOperationStatus.FAILED,
                StrmOperationStatus.TIMEOUT,
                StrmOperationStatus.CANCELLED,
            }:
                return None
            return _summary(operation)

    async def _commit(self, operation: StrmOperation) -> None:
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation.id)
            if current is None:
                raise StrmOperationNotFound(operation.id)
            if (
                current.status
                in {
                    StrmOperationStatus.SUCCEEDED,
                    StrmOperationStatus.FAILED,
                    StrmOperationStatus.TIMEOUT,
                    StrmOperationStatus.CANCELLED,
                }
                and current.status != operation.status
            ):
                _copy_state(operation, current)
                return
            current.status = operation.status
            current.started_at = operation.started_at
            current.finished_at = operation.finished_at
            current.updated_at = operation.updated_at
            current.error_code = operation.error_code
            current.generated = operation.generated
            current.unchanged = operation.unchanged
            current.skipped = operation.skipped
            current.failed = operation.failed
            current.retired = operation.retired
            current.lease_owner = operation.lease_owner
            current.lease_expires_at = operation.lease_expires_at
            current.heartbeat_at = operation.heartbeat_at
            await session.commit()
            _copy_state(operation, current)

    async def _recover(
        self,
        *,
        current_time: datetime,
        error_code: str,
        cutoff: datetime | None,
        respect_lease: bool,
    ) -> int:
        query = select(StrmOperation).where(
            StrmOperation.status.in_(
                (StrmOperationStatus.QUEUED, StrmOperationStatus.RUNNING)
            )
        )
        if cutoff is not None:
            query = query.where(
                or_(
                    and_(
                        StrmOperation.status == StrmOperationStatus.QUEUED,
                        StrmOperation.updated_at <= cutoff,
                    ),
                    and_(
                        StrmOperation.status == StrmOperationStatus.RUNNING,
                        or_(
                            StrmOperation.lease_expires_at <= current_time,
                            and_(
                                StrmOperation.lease_expires_at.is_(None),
                                StrmOperation.updated_at <= cutoff,
                            ),
                        ),
                    ),
                )
            )
        elif respect_lease:
            query = query.where(
                or_(
                    StrmOperation.lease_expires_at.is_(None),
                    StrmOperation.lease_expires_at <= current_time,
                )
            )
        async with self._session_factory() as session:
            operations = list((await session.scalars(query)).all())
            recovered = 0
            for operation in operations:
                terminal_status = (
                    StrmOperationStatus.TIMEOUT
                    if error_code == "strm_operation_timeout"
                    else StrmOperationStatus.FAILED
                )
                result = await session.execute(
                    update(StrmOperation)
                    .where(
                        StrmOperation.id == operation.id,
                        StrmOperation.status.in_(
                            (StrmOperationStatus.QUEUED, StrmOperationStatus.RUNNING)
                        ),
                        StrmOperation.updated_at == operation.updated_at,
                    )
                    .execution_options(synchronize_session=False)
                    .values(
                        status=terminal_status,
                        error_code=error_code,
                        started_at=operation.started_at or current_time,
                        finished_at=current_time,
                        updated_at=current_time,
                        lease_owner=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                    )
                )
                recovered += int(result.rowcount == 1)
            if recovered:
                await session.commit()
            else:
                await session.rollback()
            return recovered


def _summary(operation: StrmOperation) -> StrmOperationSummary:
    return StrmOperationSummary(
        operation_id=operation.id,
        library_id=operation.library_id,
        source_scan_run_id=operation.source_scan_run_id,
        kind=operation.kind.value,
        status=operation.status.value,
        workflow_id=operation.workflow_id,
        generated=operation.generated,
        unchanged=operation.unchanged,
        skipped=operation.skipped,
        failed=operation.failed,
        retired=operation.retired,
        error_code=operation.error_code,
        created_at=_as_utc(operation.created_at),
        started_at=_as_utc(operation.started_at),
        finished_at=_as_utc(operation.finished_at),
    )


def _set_counts(
    operation: StrmOperation,
    generated: int,
    unchanged: int,
    skipped: int,
    failed: int,
    retired: int,
) -> None:
    operation.generated = generated
    operation.unchanged = unchanged
    operation.skipped = skipped
    operation.failed = failed
    operation.retired = retired


def _copy_state(target: StrmOperation, source: StrmOperation) -> None:
    target.status = source.status
    target.started_at = source.started_at
    target.finished_at = source.finished_at
    target.updated_at = source.updated_at
    target.lease_owner = source.lease_owner
    target.lease_expires_at = source.lease_expires_at
    target.heartbeat_at = source.heartbeat_at
    target.error_code = source.error_code
    target.generated = source.generated
    target.unchanged = source.unchanged
    target.skipped = source.skipped
    target.failed = source.failed
    target.retired = source.retired


def _validate_counts(*values: int) -> None:
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise StrmOperationError("invalid_operation_counts")


def _validate_error_code(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise StrmOperationError("invalid_error_code")


def _validate_idempotency_key(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value != value.strip()
        or not value.isascii()
    ):
        raise StrmOperationError("invalid_idempotency_key")


def _idempotent_operation_id(
    library_id: str,
    kind: StrmOperationKind,
    idempotency_key: str,
) -> str:
    canonical = json.dumps(
        {
            "idempotency_key": idempotency_key,
            "kind": kind.value,
            "library_id": library_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "strm_op_" + hashlib.sha256(canonical).hexdigest()[:56]


def _ensure_same_request(
    operation: StrmOperation,
    *,
    library_id: str,
    source_scan_run_id: str,
    kind: StrmOperationKind,
    workflow_id: str | None,
) -> None:
    if (
        operation.library_id != library_id
        or operation.source_scan_run_id != source_scan_run_id
        or operation.kind != kind
        or operation.workflow_id != workflow_id
    ):
        raise StrmOperationError("idempotency_key_conflict")


def _claim_lock(
    session_factory: async_sessionmaker[AsyncSession], scope_id: str
) -> asyncio.Lock:
    key = (id(session_factory), scope_id)
    lock = _CLAIM_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CLAIM_LOCKS[key] = lock
    return lock


def _validate_lease_duration(value: object) -> timedelta:
    if (
        not isinstance(value, timedelta)
        or value <= timedelta(0)
        or value > timedelta(hours=24)
    ):
        raise StrmOperationError("invalid_operation_lease")
    return value


def _validate_optional_owner(value: object) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or not value.isascii()
        or "/" in value
        or "\\" in value
    ):
        raise StrmOperationError("invalid_operation_lease_owner")


def _clear_lease(operation: StrmOperation) -> None:
    operation.lease_owner = None
    operation.lease_expires_at = None
    operation.heartbeat_at = None


def _encode_cursor(summary: StrmOperationSummary) -> str:
    created_at = _as_utc(summary.created_at)
    if created_at is None:
        raise StrmOperationError("invalid_cursor")
    payload = {
        "created_at": created_at.isoformat(),
        "id": summary.operation_id,
        "v": _CURSOR_VERSION,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    ).decode("ascii").rstrip("=")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        raise StrmOperationError("invalid_cursor")
    return encoded


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > _MAX_CURSOR_LENGTH
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in cursor)
    ):
        raise StrmOperationError("invalid_cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("ascii"))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        operation_id = payload.get("id")
        created_at_value = payload.get("created_at")
        _validate_identifier(operation_id, "operation_id", maximum=64)
        if not isinstance(created_at_value, str):
            raise TypeError
        created_at = datetime.fromisoformat(created_at_value)
    except StrmOperationError:
        raise StrmOperationError("invalid_cursor") from None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        raise StrmOperationError("invalid_cursor") from None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at.astimezone(UTC), operation_id


def _validate_identifier(value: object, field: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isascii()
        or "/" in value
        or "\\" in value
    ):
        raise StrmOperationError(f"invalid_{field}")


def _is_database_lock(error: OperationalError) -> bool:
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "StrmOperationError",
    "StrmOperationKind",
    "StrmOperationNotFound",
    "StrmOperationService",
    "StrmOperationStatus",
    "StrmOperationSummary",
]
