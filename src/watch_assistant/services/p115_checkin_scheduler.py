"""每日 115 签到调度器:到配置时刻且今日未签时执行一次,失败不重试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from watch_assistant.schemas import LoggingLevel
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.p115_checkin import CheckInUnavailable, P115CheckInService
from watch_assistant.services.settings import SettingsService

_WARNING = LoggingLevel.WARNING


class P115CheckInScheduler:
    def __init__(
        self,
        service: P115CheckInService,
        *,
        event_logger: EventLogger | None = None,
        timezone: str = "Asia/Hong_Kong",
        check_in_time: str = "00:05",
        settings_service: SettingsService | None = None,
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._event_logger = event_logger
        self._timezone = timezone
        self._check_in_time = check_in_time
        self._settings_service = settings_service
        self._clock = _clock
        self._run_lock = asyncio.Lock()
        # 进程内兜底:即使 settings 持久化失败,同日门控在本进程内仍生效。
        self._last_attempt_date: str | None = None

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(ZoneInfo(self._timezone))

    def _due(self, now: datetime) -> bool:
        hour, minute = (int(p) for p in self._check_in_time.split(":"))
        return (now.hour, now.minute) >= (hour, minute)

    async def _load_last_attempt_date(self) -> str | None:
        if self._settings_service is not None:
            try:
                return await self._settings_service.get_p115_checkin_last_attempt_date()
            except Exception:  # noqa: BLE001 - gate read failure falls back in-memory
                return self._last_attempt_date
        return self._last_attempt_date

    async def _save_last_attempt_date(self, date: str) -> None:
        self._last_attempt_date = date
        if self._settings_service is not None:
            try:
                await self._settings_service.set_p115_checkin_last_attempt_date(date)
            except Exception:  # noqa: BLE001, S110 - persistence is best-effort
                pass

    async def run_due_once(self) -> bool:
        async with self._run_lock:
            now = self._now()
            if not self._due(now):
                return False
            today = now.date().isoformat()
            if await self._load_last_attempt_date() == today:
                return False  # 当日已尝试(成功或失败),本日不再重试
            try:
                status = await self._service.status()
                if status["is_sign_today"]:
                    await self._save_last_attempt_date(today)
                    return False
            except CheckInUnavailable:
                pass  # 状态读失败仍尝试签到;115 幂等保证不重复奖励
            try:
                result = await self._service.check_in()
            except CheckInUnavailable as exc:
                await self._save_last_attempt_date(today)
                await emit_event(
                    self._event_logger,
                    "p115.checkin.failed",
                    level=_WARNING,
                    fields={"error_code": exc.code},
                )
                return False
            await self._save_last_attempt_date(today)
            await emit_event(
                self._event_logger,
                "p115.checkin.succeeded",
                fields={
                    "points": result.points,
                    "continuous_day": result.continuous_day,
                },
            )
            return True

    async def run_forever(self, stop_event: asyncio.Event, *, interval_seconds: float = 300) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001, S112 - scheduler loop must stay alive
                continue
