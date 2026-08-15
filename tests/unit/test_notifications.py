from datetime import UTC, datetime

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Notification, NotificationPreference
from watch_assistant.schemas import NotificationSeverity
from watch_assistant.services.notifications import (
    NotificationService,
    _quiet_hours_suppress,
)


def _preference(**overrides):
    values = {
        "id": "default",
        "enabled": True,
        "muted_event_codes_json": "[]",
        "quiet_hours_enabled": True,
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "08:00",
        "quiet_hours_timezone": "Asia/Shanghai",
        "error_bypass_quiet_hours": True,
        "revision": 1,
    }
    values.update(overrides)
    return NotificationPreference(**values)


def test_quiet_hours_use_configured_timezone_and_overnight_window():
    during_quiet_hours = datetime(2026, 7, 31, 15, 30, tzinfo=UTC)
    during_day = datetime(2026, 7, 31, 4, 30, tzinfo=UTC)

    assert _quiet_hours_suppress(
        _preference(), NotificationSeverity.INFO, during_quiet_hours
    )
    assert not _quiet_hours_suppress(
        _preference(), NotificationSeverity.INFO, during_day
    )


def test_error_and_security_notifications_can_bypass_quiet_hours():
    now = datetime(2026, 7, 31, 15, 30, tzinfo=UTC)
    preference = _preference()

    assert not _quiet_hours_suppress(preference, NotificationSeverity.ERROR, now)
    assert not _quiet_hours_suppress(preference, NotificationSeverity.SECURITY, now)
    assert _quiet_hours_suppress(
        _preference(error_bypass_quiet_hours=False), NotificationSeverity.ERROR, now
    )


def test_checkin_events_bypass_quiet_hours():
    # UTC 15:30 = 上海 23:30,落在默认静默时段(23:00-08:00)内。
    now = datetime(2026, 7, 31, 15, 30, tzinfo=UTC)
    preference = _preference()

    assert not _quiet_hours_suppress(
        preference, NotificationSeverity.INFO, now, event_code="p115.checkin.succeeded"
    )
    assert not _quiet_hours_suppress(
        preference, NotificationSeverity.WARNING, now, event_code="p115.checkin.failed"
    )
    # 其他普通 INFO 事件在静默时段仍被抑制。
    assert _quiet_hours_suppress(
        preference, NotificationSeverity.INFO, now, event_code="task.submitted"
    )


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def log_event(self, event, **kwargs):
        self.events.append((event, kwargs))


@pytest.mark.asyncio
async def test_subscription_auto_paused_generates_actionable_notification(tmp_path):
    """subscription.auto_paused 事件应产生站内通知,标题含「订阅已自动暂停」,
    action_type 由 resource_type="subscription" 推导。"""
    from sqlalchemy import select

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subs-notif.db'}")
    await initialize_database(database.engine)
    logger = _FakeLogger()
    service = NotificationService(database.session_factory, event_logger=logger)
    # 显式写入并提交关闭静默时段的偏好,确保 INFO 事件不被静默时段拦下。
    async with database.session_factory() as session:
        session.add(
            NotificationPreference(
                id="default",
                enabled=True,
                revision=1,
                muted_event_codes_json="[]",
                quiet_hours_enabled=False,
            )
        )
        await session.commit()
    try:
        await service.handle_event(
            "subscription.auto_paused",
            fields={
                "status": "paused",
                "media_type": "tv",
                "season_number": 1,
            },
            resource_type="subscription",
            resource_id="sub_auto_paused",
        )
        async with database.session_factory() as session:
            rows = list((await session.scalars(select(Notification))).all())
        assert len(rows) == 1
        assert rows[0].event_code == "subscription.auto_paused"
        assert "订阅已自动暂停" in rows[0].title_zh
        assert rows[0].action_type == "subscription"
        assert rows[0].action_id == "sub_auto_paused"
        assert rows[0].severity == NotificationSeverity.INFO
        assert any(event == "notification.created" for event, _ in logger.events)
    finally:
        await database.engine.dispose()
