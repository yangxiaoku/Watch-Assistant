"""115 每日积分签到(proapi 积分系统),fail-closed。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CheckInResult:
    points: str
    continuous_day: int


class CheckInUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ClientFactory = Callable[[str], Any]


class P115CheckInService:
    def __init__(
        self,
        cookie_provider,
        *,
        event_logger=None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._cookie_provider = cookie_provider
        self._event_logger = event_logger
        self._client_factory = client_factory

    def _client(self):
        cookie = self._cookie_provider.load()
        if not cookie:
            raise CheckInUnavailable("credential_unavailable")
        if self._client_factory is not None:
            return self._client_factory(cookie)
        from p115client import P115Client

        return P115Client(cookie, console_qrcode=False)

    async def status(self) -> dict:
        try:
            client = self._client()
            response = await client.user_points_sign(app="android", async_=True)
        except CheckInUnavailable:
            raise
        except Exception as exc:  # remote detail stays opaque
            raise CheckInUnavailable("checkin_unavailable") from exc
        data = response.get("data", {}) if isinstance(response, dict) else {}
        return {
            "is_sign_today": bool(data.get("is_sign_today")),
            "continuous_day": int(data.get("continuous_day") or 0),
            "points_num": str(data.get("points_num") or ""),
        }

    async def check_in(self) -> CheckInResult:
        try:
            client = self._client()
            response = await client.user_points_sign_post(app="android", async_=True)
        except CheckInUnavailable:
            raise
        except Exception as exc:  # remote detail stays opaque
            raise CheckInUnavailable("checkin_unavailable") from exc
        if not isinstance(response, dict) or response.get("state") is not True:
            raise CheckInUnavailable("checkin_rejected")
        data = response.get("data", {}) or {}
        return CheckInResult(
            points=str(data.get("points_num") or ""),
            continuous_day=int(data.get("continuous_day") or 0),
        )
