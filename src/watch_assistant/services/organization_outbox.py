"""Local transactional outbox for organization directory dirty events."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.library_models import OrganizationPlan
from watch_assistant.models import (
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    OrganizationOperation,
)

DIRECTORY_DIRTY_EVENT_KIND = "directory_dirty"
DIRTY_PENDING = "pending"
DIRTY_RUNNING = "running"
DIRTY_CONSUMED = "consumed"
DIRTY_FAILED = "failed"
GENERATION_QUEUED = "queued"
GENERATION_RUNNING = "running"
GENERATION_DIRTY = "dirty"
GENERATION_RETRY_WAIT = "retry_wait"
GENERATION_CLEAN = "clean"
GENERATION_FAILED = "failed"


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
    queue_id: str | None = None
    generation: int | None = None


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
        library_id = await session.scalar(
            select(OrganizationPlan.library_id)
            .join(OrganizationOperation, OrganizationOperation.plan_id == OrganizationPlan.id)
            .where(OrganizationOperation.id == operation_id)
        )
        if library_id is None:
            raise OrganizationOutboxError("operation_library_missing")
        added = 0
        current = datetime.now(UTC)
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
            queue = await session.scalar(
                select(DirectoryDirtyGeneration).where(
                    DirectoryDirtyGeneration.library_id == library_id,
                    DirectoryDirtyGeneration.directory_id == directory_id,
                )
            )
            if queue is None:
                session.add(
                    DirectoryDirtyGeneration(
                        id="gen_" + uuid.uuid4().hex,
                        library_id=library_id,
                        directory_id=directory_id,
                        operation_id=operation_id,
                        generation=1,
                        status=GENERATION_QUEUED,
                        available_at=current,
                        updated_at=current,
                    )
                )
            else:
                queue.operation_id = operation_id
                queue.generation += 1
                queue.error_code = None
                queue.updated_at = current
                if queue.status == GENERATION_RUNNING:
                    queue.status = GENERATION_DIRTY
                else:
                    queue.status = GENERATION_QUEUED
                    queue.available_at = current
            added += 1
        return added

    async def claim_generation(
        self,
        session_factory,
        *,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> DirectoryDirtyLease | None:
        """Claim one coalesced directory generation and its latest event."""

        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise OrganizationOutboxError("invalid_lease_duration")
        current = _as_utc(now or datetime.now(UTC))
        expires = current + timedelta(seconds=lease_seconds)
        claimable = (GENERATION_QUEUED, GENERATION_RETRY_WAIT)
        for _ in range(3):
            async with session_factory() as session:
                queue = await session.scalar(
                    select(DirectoryDirtyGeneration)
                    .where(
                        DirectoryDirtyGeneration.available_at <= current,
                        or_(
                            DirectoryDirtyGeneration.status.in_(claimable),
                            (
                                (DirectoryDirtyGeneration.status == GENERATION_DIRTY)
                                & DirectoryDirtyGeneration.lease_token.is_(None)
                            ),
                            (
                                (DirectoryDirtyGeneration.status == GENERATION_RUNNING)
                                & (DirectoryDirtyGeneration.lease_expires_at <= current)
                            ),
                        ),
                    )
                    .order_by(
                        DirectoryDirtyGeneration.created_at,
                        DirectoryDirtyGeneration.id,
                    )
                    .limit(1)
                )
                if queue is None:
                    return None
                token = uuid.uuid4().hex
                result = await session.execute(
                    update(DirectoryDirtyGeneration)
                    .where(
                        DirectoryDirtyGeneration.id == queue.id,
                        DirectoryDirtyGeneration.generation == queue.generation,
                        or_(
                            DirectoryDirtyGeneration.status.in_(claimable),
                            (
                                (DirectoryDirtyGeneration.status == GENERATION_DIRTY)
                                & DirectoryDirtyGeneration.lease_token.is_(None)
                            ),
                            (
                                (DirectoryDirtyGeneration.status == GENERATION_RUNNING)
                                & (DirectoryDirtyGeneration.lease_expires_at <= current)
                            ),
                        ),
                    )
                    .values(
                        status=GENERATION_RUNNING,
                        claimed_generation=queue.generation,
                        attempts=DirectoryDirtyGeneration.attempts + 1,
                        lease_token=token,
                        lease_expires_at=expires,
                        updated_at=current,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                event = await session.scalar(
                    select(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.operation_id == queue.operation_id,
                        DirectoryDirtyEvent.directory_id == queue.directory_id,
                        DirectoryDirtyEvent.event_kind == DIRECTORY_DIRTY_EVENT_KIND,
                    )
                    .order_by(
                        DirectoryDirtyEvent.created_at.desc(),
                        DirectoryDirtyEvent.id.desc(),
                    )
                    .limit(1)
                )
                if event is None:
                    await session.execute(
                        update(DirectoryDirtyGeneration)
                        .where(DirectoryDirtyGeneration.id == queue.id)
                        .values(
                            status=GENERATION_CLEAN,
                            lease_token=None,
                            lease_expires_at=None,
                            claimed_generation=None,
                            error_code="event_missing",
                            updated_at=current,
                        )
                    )
                    await session.commit()
                    return None
                await session.execute(
                    update(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.directory_id == queue.directory_id,
                        DirectoryDirtyEvent.event_kind == DIRECTORY_DIRTY_EVENT_KIND,
                        DirectoryDirtyEvent.status == DIRTY_PENDING,
                        DirectoryDirtyEvent.operation_id.in_(
                            select(OrganizationOperation.id)
                            .join(
                                OrganizationPlan,
                                OrganizationPlan.id == OrganizationOperation.plan_id,
                            )
                            .where(OrganizationPlan.library_id == queue.library_id)
                        ),
                    )
                    .values(
                        status=DIRTY_RUNNING,
                        attempts=DirectoryDirtyEvent.attempts + 1,
                        lease_token=token,
                        lease_expires_at=expires,
                        updated_at=current,
                    )
                )
                await session.commit()
                return DirectoryDirtyLease(
                    event.id,
                    queue.operation_id,
                    queue.directory_id,
                    token,
                    queue.attempts + 1,
                    expires,
                    queue_id=queue.id,
                    generation=queue.generation,
                )
        return None

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
            if lease.queue_id is not None:
                queue = await session.get(DirectoryDirtyGeneration, lease.queue_id)
                if (
                    queue is None
                    or queue.status not in (GENERATION_RUNNING, GENERATION_DIRTY)
                    or queue.lease_token != lease.lease_token
                ):
                    await session.rollback()
                    return False
                has_newer_generation = (
                    lease.generation is not None and queue.generation > lease.generation
                )
                await session.execute(
                    update(DirectoryDirtyGeneration)
                    .where(
                        DirectoryDirtyGeneration.id == lease.queue_id,
                        DirectoryDirtyGeneration.lease_token == lease.lease_token,
                    )
                    .values(
                        status=GENERATION_QUEUED if has_newer_generation else GENERATION_CLEAN,
                        claimed_generation=None,
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=current,
                        error_code=None if status == DIRTY_CONSUMED else error_code,
                        updated_at=current,
                    )
                )
                await session.execute(
                    update(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.directory_id == lease.directory_id,
                        DirectoryDirtyEvent.lease_token == lease.lease_token,
                    )
                    .values(
                        status=status,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code=error_code,
                        updated_at=current,
                    )
                )
                await session.commit()
                return True
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
            if lease.queue_id is not None:
                queue = await session.get(DirectoryDirtyGeneration, lease.queue_id)
                if (
                    queue is None
                    or queue.status not in (GENERATION_RUNNING, GENERATION_DIRTY)
                    or queue.lease_token != lease.lease_token
                ):
                    await session.rollback()
                    return False
                has_newer_generation = (
                    lease.generation is not None and queue.generation > lease.generation
                )
                next_status = (
                    GENERATION_QUEUED
                    if has_newer_generation
                    else GENERATION_FAILED if terminal else GENERATION_RETRY_WAIT
                )
                await session.execute(
                    update(DirectoryDirtyGeneration)
                    .where(
                        DirectoryDirtyGeneration.id == lease.queue_id,
                        DirectoryDirtyGeneration.lease_token == lease.lease_token,
                    )
                    .values(
                        status=next_status,
                        claimed_generation=None,
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=current if has_newer_generation else current + timedelta(seconds=delay),
                        error_code=error_code,
                        updated_at=current,
                    )
                )
                await session.execute(
                    update(DirectoryDirtyEvent)
                    .where(
                        DirectoryDirtyEvent.directory_id == lease.directory_id,
                        DirectoryDirtyEvent.lease_token == lease.lease_token,
                    )
                    .values(
                        status=DIRTY_FAILED if terminal and not has_newer_generation else DIRTY_PENDING,
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=current + timedelta(seconds=delay),
                        error_code=error_code,
                        updated_at=current,
                    )
                )
                await session.commit()
                return True
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
    "GENERATION_CLEAN",
    "GENERATION_DIRTY",
    "GENERATION_FAILED",
    "GENERATION_QUEUED",
    "GENERATION_RETRY_WAIT",
    "DirectoryDirtyLease",
    "DirectoryDirtyOutboxService",
    "OrganizationOutboxError",
]
