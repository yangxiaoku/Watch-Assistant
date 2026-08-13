import pytest

from watch_assistant.services.p115_checkin import (
    CheckInUnavailable,
    P115CheckInService,
)


class _FakeCookieProvider:
    def __init__(self, cookie: str | None):
        self._cookie = cookie
    def load(self) -> str | None:
        return self._cookie


class _FakeClient:
    def __init__(self, sign_post=None, sign=None):
        self._sign_post = sign_post or {"state": True, "data": {"points_num": "7", "continuous_day": 7}}
        self._sign = sign or {"state": True, "data": {"is_sign_today": 1, "continuous_day": 7}}
    async def user_points_sign_post(self, app="android", async_=True):
        return self._sign_post
    async def user_points_sign(self, app="android", async_=True):
        return self._sign


@pytest.mark.asyncio
async def test_check_in_returns_points_and_streak():
    service = P115CheckInService(_FakeCookieProvider("cookie"), client_factory=lambda c: _FakeClient())
    result = await service.check_in()
    assert result.points == "7"
    assert result.continuous_day == 7


@pytest.mark.asyncio
async def test_check_in_missing_cookie_raises_credential_unavailable():
    service = P115CheckInService(_FakeCookieProvider(None), client_factory=lambda c: _FakeClient())
    with pytest.raises(CheckInUnavailable) as exc:
        await service.check_in()
    assert exc.value.code == "credential_unavailable"


@pytest.mark.asyncio
async def test_check_in_rejected_state_raises():
    client = _FakeClient(sign_post={"state": False, "error": "rejected"})
    service = P115CheckInService(_FakeCookieProvider("cookie"), client_factory=lambda c: client)
    with pytest.raises(CheckInUnavailable) as exc:
        await service.check_in()
    assert exc.value.code == "checkin_rejected"


@pytest.mark.asyncio
async def test_status_reports_today_signed():
    service = P115CheckInService(_FakeCookieProvider("cookie"), client_factory=lambda c: _FakeClient())
    status = await service.status()
    assert status["is_sign_today"] is True
    assert status["continuous_day"] == 7
