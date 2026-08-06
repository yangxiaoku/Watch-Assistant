"""Durable, paginated results of completed media organization."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import OrganizationHistoryEntry, OrganizationPlan


class OrganizationHistoryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OrganizationHistoryItem:
    id: str
    operation_id: str
    plan_id: str
    source_object_id: str
    source_directory_id: str
    target_directory_id: str
    tmdb_id: int | None
    title: str
    media_type: str | None
    source_name: str
    target_path: str
    status: str
    error_code: str | None
    completed_at: datetime

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "operation_id": self.operation_id,
            "plan_id": self.plan_id,
            "source_object_id": self.source_object_id,
            "source_directory_id": self.source_directory_id,
            "target_directory_id": self.target_directory_id,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "media_type": self.media_type,
            "source_name": self.source_name,
            "target_path": self.target_path,
            "status": self.status,
            "error_code": self.error_code,
            "completed_at": _utc(self.completed_at),
        }


class OrganizationHistoryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_items(
        self,
        *,
        cursor: int = 0,
        limit: int = 50,
        library_ids: Collection[str] | None = None,
    ) -> tuple[list[OrganizationHistoryItem], int | None]:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise OrganizationHistoryError("invalid_pagination")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OrganizationHistoryError("invalid_pagination")
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        _history_query(library_ids)
                        .order_by(
                            OrganizationHistoryEntry.completed_at.desc(),
                            OrganizationHistoryEntry.id.desc(),
                        )
                        .offset(cursor)
                        .limit(limit)
                    )
                ).all()
            )
        items = [_item(row) for row in rows]
        return items, (cursor + len(items) if len(items) == limit else None)

    async def get(
        self,
        item_id: str,
        *,
        library_ids: Collection[str] | None = None,
    ) -> OrganizationHistoryItem:
        if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
            raise OrganizationHistoryError("invalid_history_id")
        async with self._session_factory() as session:
            row = await session.scalar(
                _history_query(library_ids).where(
                    OrganizationHistoryEntry.id == item_id
                )
            )
        if row is None:
            raise OrganizationHistoryError("history_not_found")
        return _item(row)


def _history_query(library_ids: Collection[str] | None):
    query = select(OrganizationHistoryEntry).join(
        OrganizationPlan, OrganizationPlan.id == OrganizationHistoryEntry.plan_id
    )
    if library_ids is not None:
        query = query.where(OrganizationPlan.library_id.in_(library_ids))
    return query


def _item(row: OrganizationHistoryEntry) -> OrganizationHistoryItem:
    return OrganizationHistoryItem(
        id=row.id,
        operation_id=row.operation_id,
        plan_id=row.plan_id,
        source_object_id=row.source_object_id,
        source_directory_id=row.source_directory_id,
        target_directory_id=row.target_directory_id,
        tmdb_id=row.tmdb_id,
        title=row.title,
        media_type=row.media_type,
        source_name=row.source_name,
        target_path=row.target_path,
        status=row.status,
        error_code=row.error_code,
        completed_at=_utc(row.completed_at),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["OrganizationHistoryError", "OrganizationHistoryItem", "OrganizationHistoryService"]
