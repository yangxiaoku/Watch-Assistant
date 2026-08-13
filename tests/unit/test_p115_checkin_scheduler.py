from datetime import UTC, datetime

import pytest

from watch_assistant.schemas import P115CheckInSettingsResponse
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


class _FakeSettings:
    def __init__(self, enabled=False, check_in_time="00:05", last_attempt_date=None):
        self.enabled = enabled
        self.check_in_time = check_in_time
        self.last_attempt_date = last_attempt_date
    async def get_p115_checkin(self):
        return P115CheckInSettingsResponse(enabled=self.enabled, check_in_time=self.check_in_time)
    async def get_p115_checkin_last_attempt_date(self):
        return self.last_attempt_date
    async def set_p115_checkin_last_attempt_date(self, date):
        self.last_attempt_date = date


async def _run_due(scheduler):
    return await scheduler.run_due_once()


@pytest.mark.asyncio
async def test_scheduler_skips_before_target_time():
    service = _FakeService()
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=_Clock(datetime(2026, 8, 13, 0, 0, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_fires_after_target_when_not_signed():
    service = _FakeService()
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is True
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_skips_when_already_signed_today():
    service = _FakeService(status={"is_sign_today": True, "continuous_day": 7, "points_num": "7"})
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_records_failure_event_without_raising():
    service = _FakeService(fail=True)
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_does_not_retry_same_day_after_failure():
    service = _FakeService(fail=True)
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 1
    # 同日后续轮询:当日已尝试门控生效,不再发起第二次签到。
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_retries_after_midnight():
    service = _FakeService(fail=True)
    clock = _Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        _clock=clock,
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 1
    # 跨天后时钟进入次日,允许再次尝试。
    clock.now = datetime(2026, 8, 14, 0, 6, tzinfo=UTC)
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 2


@pytest.mark.asyncio
async def test_scheduler_persists_attempt_date_across_restart():
    settings = _FakeSettings()
    service = _FakeService(fail=True)
    clock = _Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        settings_service=settings,
        _clock=clock,
    )
    assert await _run_due(scheduler) is False
    assert settings.last_attempt_date == "2026-08-13"
    # 模拟调度器重启:新实例读取同一持久化状态,同日不再尝试。
    service2 = _FakeService(fail=True)
    scheduler2 = P115CheckInScheduler(
        service2,
        timezone="Asia/Hong_Kong",
        check_in_time="00:05",
        env_enabled=True,
        settings_service=settings,
        _clock=clock,
    )
    assert await _run_due(scheduler2) is False
    assert service2.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_disabled_when_env_and_stored_both_false():
    service = _FakeService()
    settings = _FakeSettings(enabled=False)
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        env_enabled=False,
        settings_service=settings,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is False
    assert service.status_calls == 0
    assert service.check_in_calls == 0


@pytest.mark.asyncio
async def test_scheduler_fires_when_stored_enabled_but_env_disabled():
    service = _FakeService()
    settings = _FakeSettings(enabled=True)
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        env_enabled=False,
        settings_service=settings,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is True
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_fires_when_env_enabled_but_stored_disabled():
    service = _FakeService()
    settings = _FakeSettings(enabled=False)
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        env_enabled=True,
        settings_service=settings,
        _clock=_Clock(datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    assert await _run_due(scheduler) is True
    assert service.check_in_calls == 1


@pytest.mark.asyncio
async def test_scheduler_uses_env_check_in_time_when_no_settings_service():
    service = _FakeService()
    clock = _Clock(datetime(2026, 8, 13, 0, 30, tzinfo=UTC))
    scheduler = P115CheckInScheduler(
        service,
        timezone="Asia/Hong_Kong",
        env_enabled=True,
        env_check_in_time="01:00",
        _clock=clock,
    )
    assert await _run_due(scheduler) is False
    assert service.check_in_calls == 0
    # 到达 env 时刻后触发。
    clock.now = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)
    assert await _run_due(scheduler) is True
    assert service.check_in_calls == 1
