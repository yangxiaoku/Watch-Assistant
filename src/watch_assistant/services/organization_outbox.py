"""Local transactional outbox for organization directory dirty events."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.models import DirectoryDirtyEvent

DIRECTORY_DIRTY_EVENT_KIND = "directory_dirty"
DIRTY_PENDING = "pending"
DIRTY_RUNNING = "running"
DIRTY_CONSUMED = "consumed"
DIRTY_FAILED = "failed"


class OrganizationOutboxError(ValueError):
    """Stable local outbox validation or persistence boundary error."""


@dataclass(frozen=True, slots=True)
class DirectoryDirtyLease:
    event_id: str
    operation_id: str
    directory_id: str
    lease_token: str
    attempts: int
    lease_expires_at: datetime


class DirectoryDirtyOutboxService:
    """Add deduplicated pending events to a caller-owned transaction."""

    async def enqueue_directory_dirty(
        self,
        session: AsyncSession,
        *,
        operation_id: str,
        directory_ids: Iterable[str],
    ) -> int:
        _validate_identifier(operation_id, "invalid_operation_id", maximum=40)
        normalized = _normalize_directory_ids(directory_ids)
        if not normalized:
            raise OrganizationOutboxError("invalid_directory_scope")

        existing = set(
            await session.scalars(
                select(DirectoryDirtyEvent.directory_id).where(
                    DirectoryDirtyEvent.operation_id == operation_id,
                    DirectoryDirtyEvent.event_kind == DIRECTORY_DIRTY_EVENT_KIND,
                )
            )
        )
        added = 0
        for directory_id in normalized:
            if directory_id in existing:
                continue
            session.add(
                DirectoryDirtyEvent(
                    id="evt_" + uuid.uuid4().hex,
                    operation_id=operation_id,
                    directory_id=directory_id,
                    event_kind=DIRECTORY_DIRTY_EVENT_KIND,
                    status="pending",
                )
            )
            added += 1
        return added

    async def claim_next(
        self,
        session_factory,
        *,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> DirectoryDirtyLease | None:
        """Atomically claim one pending or expired event."""

        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise OrganizationOutboxError("invalid_lease_duration")
        current = _as_utc(now or datetime.now(UTC))
        expires = current + timedelta(seconds=lease_seconds)
        for _ in range(3):
            async with session_factory() as session:
                event = await session.scalar(
                    select(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.event_kind == DIRECTORY_DIRTY_EVENT_KIND,
                        DirectoryDirtyEvent.available_at <= current,
                        or_(
                            DirectoryDirtyEvent.status == DIRTY_PENDING,
                            (
                                (DirectoryDirtyEvent.status == DIRTY_RUNNING)
                                & (DirectoryDirtyEvent.lease_expires_at <= current)
                            ),
                        ),
                    )
                    .order_by(DirectoryDirtyEvent.created_at, DirectoryDirtyEvent.id)
                    .limit(1)
                )
                if event is None:
                    return None
                token = uuid.uuid4().hex
                result = await session.execute(
                    update(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.id == event.id,
                        DirectoryDirtyEvent.status.in_((DIRTY_PENDING, DIRTY_RUNNING)),
                        or_(
                            DirectoryDirtyEvent.status == DIRTY_PENDING,
                            DirectoryDirtyEvent.lease_expires_at <= current,
                        ),
                    )
                    .values(
                        status=DIRTY_RUNNING,
                        attempts=DirectoryDirtyEvent.attempts + 1,
                        lease_token=token,
                        lease_expires_at=expires,
                        updated_at=current,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return DirectoryDirtyLease(
                    event.id,
                    event.operation_id,
                    event.directory_id,
                    token,
                    event.attempts + 1,
                    expires,
                )
        return None

    async def complete(
        self,
        session_factory,
        lease: DirectoryDirtyLease,
        *,
        status: str = DIRTY_CONSUMED,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status not in {DIRTY_CONSUMED, DIRTY_FAILED}:
            raise OrganizationOutboxError("invalid_terminal_status")
        _validate_error_code(error_code)
        current = _as_utc(now or datetime.now(UTC))
        async with session_factory() as session:
            result = await session.execute(
                update(DirectoryDirtyEvent)
                .where(
                    DirectoryDirtyEvent.id == lease.event_id,
                    DirectoryDirtyEvent.status == DIRTY_RUNNING,
                    DirectoryDirtyEvent.lease_token == lease.lease_token,
                )
                .values(
                    status=status,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=error_code,
                    updated_at=current,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount == 1

    async def retry(
        self,
        session_factory,
        lease: DirectoryDirtyLease,
        *,
        error_code: str,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> bool:
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise OrganizationOutboxError("invalid_max_attempts")
        _validate_error_code(error_code)
        current = _as_utc(now or datetime.now(UTC))
        terminal = lease.attempts >= max_attempts
        delay = min(3600, 2 ** max(0, lease.attempts - 1))
        async with session_factory() as session:
            result = await session.execute(
                update(DirectoryDirtyEvent)
                .where(
                    DirectoryDirtyEvent.id == lease.event_id,
                    DirectoryDirtyEvent.status == DIRTY_RUNNING,
                    DirectoryDirtyEvent.lease_token == lease.lease_token,
                )
                .values(
                    status=DIRTY_FAILED if terminal else DIRTY_PENDING,
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=current + timedelta(seconds=delay),
                    error_code=error_code,
                    updated_at=current,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount == 1


def _normalize_directory_ids(directory_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(directory_ids, str):
        raise OrganizationOutboxError("invalid_directory_scope")
    values: list[str] = []
    seen: set[str] = set()
    try:
        items = tuple(directory_ids)
    except TypeError:
        raise OrganizationOutboxError("invalid_directory_scope") from None
    for directory_id in items:
        _validate_identifier(directory_id, "invalid_directory_id", maximum=128)
        if directory_id not in seen:
            seen.add(directory_id)
            values.append(directory_id)
    return tuple(values)


def _validate_identifier(value: str, error: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(
            character in value
            for character in ("/", "\\", "://", "=", "?", "#", "\x00")
        )
    ):
        raise OrganizationOutboxError(error)


def _validate_error_code(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in value)
    ):
        raise OrganizationOutboxError("invalid_error_code")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "DIRECTORY_DIRTY_EVENT_KIND",
    "DIRTY_CONSUMED",
    "DIRTY_FAILED",
    "DIRTY_PENDING",
    "DIRTY_RUNNING",
    "DirectoryDirtyLease",
    "DirectoryDirtyOutboxService",
    "OrganizationOutboxError",
]
