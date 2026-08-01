"""Durable local ledger for STRM synchronization operations."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select
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


_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 256


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
        if operation.status in {
            StrmOperationStatus.SUCCEEDED,
            StrmOperationStatus.FAILED,
        }:
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
        if not isinstance(error_code, str) or not error_code or len(error_code) > 100:
            raise StrmOperationError("invalid_error_code")
        operation = await self._load(operation_id)
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
        return _summary(await self._load(operation_id))

    async def recover_incomplete(
        self,
        *,
        now: datetime | None = None,
        error_code: str = "strm_operation_recovered",
    ) -> int:
        """Terminalize operations left behind by a cancelled process."""

        _validate_error_code(error_code)
        current_time = _as_utc(now) or datetime.now(UTC)
        return await self._recover(
            current_time=current_time,
            error_code=error_code,
            cutoff=None,
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

    async def _recover(
        self,
        *,
        current_time: datetime,
        error_code: str,
        cutoff: datetime | None,
    ) -> int:
        query = select(StrmOperation).where(
            StrmOperation.status.in_(
                (StrmOperationStatus.QUEUED, StrmOperationStatus.RUNNING)
            )
        )
        if cutoff is not None:
            query = query.where(StrmOperation.updated_at <= cutoff)
        async with self._session_factory() as session:
            operations = list((await session.scalars(query)).all())
            for operation in operations:
                operation.status = StrmOperationStatus.FAILED
                operation.error_code = error_code
                operation.started_at = operation.started_at or current_time
                operation.finished_at = current_time
                operation.updated_at = current_time
            if operations:
                await session.commit()
            return len(operations)


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
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise StrmOperationError("invalid_operation_counts")


def _validate_error_code(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise StrmOperationError("invalid_error_code")


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
