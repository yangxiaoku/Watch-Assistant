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
        manual_run: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._settings = settings
        self._run_once = run_once
        self._manual_run = manual_run or run_once
        self._wake = asyncio.Event()
        self._manual_runs = 0

    async def request_run_now(self) -> None:
        self._manual_runs += 1
        self._wake.set()

    def notify_settings_changed(self) -> None:
        """Wake the loop so a saved interval or schedule flag is re-read."""

        self._wake.set()

    def stop_pending(self) -> None:
        self._manual_runs = 0
        self._wake.set()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        next_scheduled_at: float | None = None
        while not stop_event.is_set():
            if self._manual_runs:
                self._manual_runs -= 1
                await self._manual_run()
                continue
            current = await self._settings.get_organization()
            now = time.monotonic()
            if not current.schedule_enabled:
                next_scheduled_at = None
            elif next_scheduled_at is None:
                next_scheduled_at = now + current.scan_interval_minutes * 60
            elif now >= next_scheduled_at:
                await self._run_once()
                next_scheduled_at = time.monotonic() + current.scan_interval_minutes * 60
                continue
            timeout = (
                None
                if next_scheduled_at is None
                else max(0.1, next_scheduled_at - now)
            )
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue


__all__ = ["OrganizationScheduler"]
