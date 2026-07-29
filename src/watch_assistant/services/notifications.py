"""In-app notification persistence, deduplication, and read state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Notification, NotificationPreference
from watch_assistant.schemas import (
    NotificationListResponse,
    NotificationPreferencePatch,
    NotificationPreferenceResponse,
    NotificationResponse,
    NotificationSeverity,
)
from watch_assistant.services.observability import EventLogger, emit_event

DEDUPLICATION_WINDOW = timedelta(minutes=30)


class NotificationNotFound(LookupError):
    pass


class NotificationConflict(ValueError):
    pass


class NotificationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def notify(
        self,
        *,
        event_code: str,
        severity: NotificationSeverity,
        title_zh: str,
        message_zh: str,
        dedupe_key: str,
        action_type: str | None = None,
        action_id: str | None = None,
    ) -> NotificationResponse | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            preference = await self._preference(session)
            if not preference.enabled or event_code in _decode_codes(
                preference.muted_event_codes_json
            ):
                return None
            existing = await session.scalar(
                select(Notification)
                .where(
                    Notification.dedupe_key == dedupe_key,
                    Notification.created_at >= now - DEDUPLICATION_WINDOW,
                )
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                existing.aggregate_count += 1
                existing.updated_at = now
                await session.commit()
                response = _response(existing)
                event = "notification.aggregated"
                event_count = existing.aggregate_count
            else:
                item = Notification(
                    id="notification_" + uuid4().hex,
                    event_code=event_code,
                    severity=severity,
                    title_zh=title_zh,
                    message_zh=message_zh,
                    action_type=action_type,
                    action_id=action_id,
                    dedupe_key=dedupe_key,
                    aggregate_count=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
                await session.commit()
                response = _response(item)
                event = "notification.created"
                event_count = 1
        await emit_event(
            self._event_logger,
            event,
            fields={"status": severity.value, "count": event_count},
            task_id=action_id if action_type == "task" else None,
        )
        return response

    async def list(
        self, *, unread_only: bool = False, limit: int = 50, offset: int = 0
    ) -> NotificationListResponse:
        # MCP asks for one look-ahead row to produce a stable next cursor.
        limit = max(1, min(limit, 101))
        offset = max(0, offset)
        async with self._session_factory() as session:
            query = (
                select(Notification)
                .order_by(Notification.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            if unread_only:
                query = query.where(Notification.read_at.is_(None))
            items = list(await session.scalars(query))
            unread_count = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.read_at.is_(None))
            )
        return NotificationListResponse(
            items=[_response(item) for item in items],
            unread_count=int(unread_count or 0),
        )

    async def mark_read(self, notification_id: str) -> NotificationResponse:
        async with self._session_factory() as session:
            item = await session.get(Notification, notification_id)
            if item is None:
                raise NotificationNotFound(notification_id)
            if item.read_at is None:
                item.read_at = datetime.now(UTC)
                await session.commit()
            response = _response(item)
        await emit_event(self._event_logger, "notification.read", task_id=item.action_id)
        return response

    async def mark_all_read(self) -> int:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(
                    select(Notification).where(Notification.read_at.is_(None))
                )
            )
            now = datetime.now(UTC)
            for item in items:
                item.read_at = now
            await session.commit()
            count = len(items)
        await emit_event(
            self._event_logger,
            "notification.read_all",
            fields={"count": count},
        )
        return count

    async def get_preferences(self) -> NotificationPreferenceResponse:
        async with self._session_factory() as session:
            return _preference_response(await self._preference(session))

    async def update_preferences(
        self, patch: NotificationPreferencePatch
    ) -> NotificationPreferenceResponse:
        async with self._session_factory() as session:
            preference = await self._preference(session)
            if preference.revision != patch.revision:
                raise NotificationConflict("notification_preference_conflict")
            if patch.enabled is not None:
                preference.enabled = patch.enabled
            if patch.muted_event_codes is not None:
                preference.muted_event_codes_json = json.dumps(
                    sorted(set(patch.muted_event_codes)),
                    ensure_ascii=False,
                )
            preference.revision += 1
            await session.commit()
            response = _preference_response(preference)
        await emit_event(
            self._event_logger,
            "notification.preferences_changed",
            fields={"status": "enabled" if response.enabled else "disabled"},
        )
        return response

    @staticmethod
    async def _preference(session: AsyncSession) -> NotificationPreference:
        item = await session.get(NotificationPreference, "default")
        if item is None:
            item = NotificationPreference(id="default", enabled=True, revision=1)
            session.add(item)
            await session.flush()
        return item


def _decode_codes(value: str) -> set[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return set()
    return {item for item in decoded if isinstance(item, str)} if isinstance(decoded, list) else set()


def _response(item: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=item.id,
        event_code=item.event_code,
        severity=item.severity,
        title_zh=item.title_zh,
        message_zh=item.message_zh,
        action_type=item.action_type,
        action_id=item.action_id,
        aggregate_count=item.aggregate_count,
        read_at=item.read_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _preference_response(item: NotificationPreference) -> NotificationPreferenceResponse:
    return NotificationPreferenceResponse(
        enabled=item.enabled,
        muted_event_codes=sorted(_decode_codes(item.muted_event_codes_json)),
        revision=item.revision,
    )
