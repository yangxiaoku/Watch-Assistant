from datetime import UTC, datetime

import pytest

from watch_assistant.services.p115_checkin import CheckInResult, CheckInUnavailable
from watch_assistant.services.p115_checkin_scheduler import P115CheckInScheduler


class _FakeService:
    def __init__(self, status=None, result=None, fail=False):
        self._status = status or {"is_sign_today": False, "continuous_day": 0, "points_num": ""}
        self._result = result or CheckInResult(points="7", continuous_day=8)
        self._fail = fail
        self.check_in_calls = 0
        self.status_calls = 0
    async def status(self):
        self.status_calls += 1
        return self._status
    async def check_in(self):
        self.check_in_calls += 1
        if self._fail:
            raise CheckInUnavailable("checkin_rejected")
        return self._result


class _Clock:
    def __init__(self, now):
        self.now = now
    def __call__(self):
        return self.now


async def _run_due(scheduler):
    return await scheduler.run_due_once()


@pytest.mark.asyncio
async def test_scheduler_skips_before_target_time():
    service = _FakeService()
    scheduler = P115CheckInScheduler(
        service, timezone="Asia/Hong_Kong", check_in_time="00:05", _clock=_Clock(datetime(2026, 8, 13, 0, 0, tzinfo=UTC))
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_fires_after_target_when_not_signed():
    service = _FakeService()
    scheduler = P115CheckInScheduler(
        service, timezone="Asia/Hong_Kong", check_in_time="00:05", _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    )
    assert await _run_due(scheduler) is True
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_skips_when_already_signed_today():
    service = _FakeService(status={"is_sign_today": True, "continuous_day": 7, "points_num": "7"})
    scheduler = P115CheckInScheduler(
        service, timezone="Asia/Hong_Kong", check_in_time="00:05", _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_records_failure_event_without_raising():
    service = _FakeService(fail=True)
    scheduler = P115CheckInScheduler(
        service, timezone="Asia/Hong_Kong", check_in_time="00:05", _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 1
