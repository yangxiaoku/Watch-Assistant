"""每日 115 签到调度器:到配置时刻且今日未签时执行一次,失败不重试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from watch_assistant.schemas import LoggingLevel
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.p115_checkin import CheckInUnavailable, P115CheckInService

_WARNING = LoggingLevel.WARNING


class P115CheckInScheduler:
    def __init__(
        self,
        service: P115CheckInService,
        *,
        event_logger: EventLogger | None = None,
        timezone: str = "Asia/Hong_Kong",
        check_in_time: str = "00:05",
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._event_logger = event_logger
        self._timezone = timezone
        self._check_in_time = check_in_time
        self._clock = _clock
        self._run_lock = asyncio.Lock()

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(ZoneInfo(self._timezone))

    def _due(self, now: datetime) -> bool:
        hour, minute = (int(p) for p in self._check_in_time.split(":"))
        return (now.hour, now.minute) >= (hour, minute)

    async def run_due_once(self) -> bool:
        async with self._run_lock:
            now = self._now()
            if not self._due(now):
                return False
            try:
                status = await self._service.status()
                if status["is_sign_today"]:
                    return False
            except CheckInUnavailable:
                pass  # 状态读失败仍尝试签到;115 幂等保证不重复奖励
            try:
                result = await self._service.check_in()
            except CheckInUnavailable as exc:
                await emit_event(
                    self._event_logger,
                    "p115.checkin.failed",
                    level=_WARNING,
                    fields={"error_code": exc.code},
                )
                return False
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
