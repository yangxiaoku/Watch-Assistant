"""Durable local ledger for STRM synchronization operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
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
    ) -> StrmOperationSummary:
        _validate_identifier(library_id, "library_id", maximum=128)
        _validate_identifier(source_scan_run_id, "source_scan_run_id", maximum=64)
        if workflow_id is not None:
            _validate_identifier(workflow_id, "workflow_id", maximum=40)
        try:
            operation_kind = StrmOperationKind(kind)
        except ValueError:
            raise StrmOperationError("invalid_operation_kind") from None
        now = datetime.now(UTC)
        operation = StrmOperation(
            id="strm_op_" + uuid4().hex,
            library_id=library_id,
            source_scan_run_id=source_scan_run_id,
            kind=operation_kind,
            workflow_id=workflow_id,
            status=StrmOperationStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(operation)
            await session.commit()
        return _summary(operation)

    async def start(self, operation_id: str) -> StrmOperationSummary:
        operation = await self._load(operation_id)
        if operation.status is StrmOperationStatus.QUEUED:
            now = datetime.now(UTC)
            operation.status = StrmOperationStatus.RUNNING
            operation.started_at = now
            operation.updated_at = now
            await self._commit(operation)
        return _summary(operation)

    async def complete(
        self,
        operation_id: str,
        *,
        generated: int,
        unchanged: int,
        skipped: int,
        failed: int,
        retired: int,
    ) -> StrmOperationSummary:
        _validate_counts(generated, unchanged, skipped, failed, retired)
        operation = await self._load(operation_id)
        if operation.status is StrmOperationStatus.SUCCEEDED:
            return _summary(operation)
        if operation.status is StrmOperationStatus.FAILED:
            return _summary(operation)
        now = datetime.now(UTC)
        _set_counts(operation, generated, unchanged, skipped, failed, retired)
        operation.status = StrmOperationStatus.SUCCEEDED
        operation.started_at = operation.started_at or now
        operation.finished_at = now
        operation.updated_at = now
        await self._commit(operation)
        return _summary(operation)

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
    ) -> StrmOperationSummary:
        _validate_counts(generated, unchanged, skipped, failed, retired)
        if not error_code or len(error_code) > 100:
            raise StrmOperationError("invalid_error_code")
        operation = await self._load(operation_id)
        # Terminal state is deliberately monotonic. A late exception cannot
        # turn a confirmed success into a failure, and repeated failure calls
        # remain safe for retry/recovery paths.
        if operation.status in {
            StrmOperationStatus.SUCCEEDED,
            StrmOperationStatus.FAILED,
        }:
            return _summary(operation)
        now = datetime.now(UTC)
        _set_counts(operation, generated, unchanged, skipped, failed, retired)
        operation.status = StrmOperationStatus.FAILED
        operation.error_code = error_code
        operation.started_at = operation.started_at or now
        operation.finished_at = now
        operation.updated_at = now
        await self._commit(operation)
        return _summary(operation)

    async def get(self, operation_id: str) -> StrmOperationSummary:
        operation = await self._load(operation_id)
        return _summary(operation)

    async def list(
        self, library_id: str, *, cursor: int = 0, limit: int = 20
    ) -> tuple[list[StrmOperationSummary], int | None]:
        _validate_identifier(library_id, "library_id", maximum=128)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise StrmOperationError("invalid_cursor")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise StrmOperationError("invalid_limit")
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(StrmOperation)
                        .where(StrmOperation.library_id == library_id)
                        .order_by(
                            StrmOperation.created_at.desc(),
                            StrmOperation.id.desc(),
                        )
                        .offset(cursor)
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return [
            _summary(operation) for operation in rows
        ], cursor + limit if has_more else None

    async def _load(self, operation_id: str) -> StrmOperation:
        _validate_identifier(operation_id, "operation_id", maximum=64)
        async with self._session_factory() as session:
            operation = await session.get(StrmOperation, operation_id)
            if operation is None:
                raise StrmOperationNotFound(operation_id)
            return operation

    async def _commit(self, operation: StrmOperation) -> None:
        async with self._session_factory() as session:
            current = await session.get(StrmOperation, operation.id)
            if current is None:
                raise StrmOperationNotFound(operation.id)
            if (
                current.status in {
                    StrmOperationStatus.SUCCEEDED,
                    StrmOperationStatus.FAILED,
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
            await session.commit()
            _copy_state(operation, current)


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
    target.error_code = source.error_code
    target.generated = source.generated
    target.unchanged = source.unchanged
    target.skipped = source.skipped
    target.failed = source.failed
    target.retired = source.retired


def _validate_counts(*values: int) -> None:
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise StrmOperationError("invalid_operation_counts")


def _validate_identifier(value: str, field: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StrmOperationError(f"invalid_{field}")


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
