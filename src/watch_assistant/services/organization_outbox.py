"""Local transactional outbox for organization directory dirty events."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.models import DirectoryDirtyEvent

DIRECTORY_DIRTY_EVENT_KIND = "directory_dirty"


class OrganizationOutboxError(ValueError):
    """Stable local outbox validation or persistence boundary error."""


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


__all__ = [
    "DIRECTORY_DIRTY_EVENT_KIND",
    "DirectoryDirtyOutboxService",
    "OrganizationOutboxError",
]
