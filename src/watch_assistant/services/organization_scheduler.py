"""Process-local trigger queue for scheduled and manual organization work."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

from watch_assistant.services.settings import SettingsService


class OrganizationScheduler:
    """Wake the organization worker without conflating manual and scheduled runs."""

    def __init__(
        self,
        settings: SettingsService,
        run_once: Callable[[], Awaitable[bool]],
        manual_run: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._settings = settings
        self._run_once = run_once
        self._manual_run = manual_run
        self._wake = asyncio.Event()
        self._manual_runs: deque[str] = deque()

    _MANUAL_RUNS_MAX = 32

    async def request_run_now(self) -> str:
        run_id = f"org_{uuid.uuid4().hex}"
        # L14: 排队中的手动整理请求设上限,防止长跑实例无界堆积。
        if len(self._manual_runs) < self._MANUAL_RUNS_MAX:
            self._manual_runs.append(run_id)
        self._wake.set()
        return run_id

    def notify_settings_changed(self) -> None:
        """Wake the loop so a saved interval or schedule flag is re-read."""

        self._wake.set()

    def stop_pending(self) -> None:
        self._manual_runs.clear()
        self._wake.set()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        next_scheduled_at: float | None = None
        # L14: 连续异常指数退避(上限 30s,成功复位),避免 run_once/manual_run
        # 抛异常使调度循环死亡,或故障下满速热循环刷爆日志。
        backoff = 0.0
        while not stop_event.is_set():
            if backoff > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                backoff = 0.0
            try:
                if self._manual_runs:
                    run_id = self._manual_runs.popleft()
                    if self._manual_run is None:
                        await self._run_once()
                    else:
                        await self._manual_run(run_id)
                    continue
                current = await self._settings.get_organization()
                # A manual request may arrive while settings are being read.
                # Check again before clearing the wake event so it cannot be lost.
                if self._manual_runs:
                    continue
                now = time.monotonic()
                if not current.schedule_enabled:
                    next_scheduled_at = None
                elif next_scheduled_at is None:
                    next_scheduled_at = now + current.scan_interval_minutes * 60
                elif now >= next_scheduled_at:
                    await self._run_once()
                    next_scheduled_at = (
                        time.monotonic() + current.scan_interval_minutes * 60
                    )
                    continue
                timeout = (
                    None
                    if next_scheduled_at is None
                    else max(0.1, next_scheduled_at - now)
                )
                self._wake.clear()
                if self._manual_runs:
                    continue
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - scheduler loop must stay alive
                del exc
                backoff = min(30.0, (backoff or 1.0) * 2)


__all__ = ["OrganizationScheduler"]
