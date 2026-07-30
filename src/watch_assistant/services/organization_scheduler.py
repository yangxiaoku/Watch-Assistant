"""Process-local trigger queue for scheduled and manual organization work."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from watch_assistant.services.settings import SettingsService


class OrganizationScheduler:
    """Wake the organization worker without conflating manual and scheduled runs."""

    def __init__(
        self,
        settings: SettingsService,
        run_once: Callable[[], Awaitable[bool]],
    ) -> None:
        self._settings = settings
        self._run_once = run_once
        self._wake = asyncio.Event()
        self._manual_runs = 0

    async def request_run_now(self) -> None:
        self._manual_runs += 1
        self._wake.set()

    def stop_pending(self) -> None:
        self._manual_runs = 0
        self._wake.set()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        next_scheduled_at = time.monotonic()
        while not stop_event.is_set():
            if self._manual_runs:
                self._manual_runs -= 1
                await self._run_once()
                continue
            current = await self._settings.get_organization()
            now = time.monotonic()
            if current.schedule_enabled and now >= next_scheduled_at:
                await self._run_once()
                next_scheduled_at = time.monotonic() + current.scan_interval_minutes * 60
                continue
            if not current.schedule_enabled:
                next_scheduled_at = now + current.scan_interval_minutes * 60
            timeout = max(0.1, next_scheduled_at - now)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue


__all__ = ["OrganizationScheduler"]
