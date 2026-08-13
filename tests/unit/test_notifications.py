from datetime import UTC, datetime

from watch_assistant.models import NotificationPreference
from watch_assistant.schemas import NotificationSeverity
from watch_assistant.services.notifications import _quiet_hours_suppress


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
