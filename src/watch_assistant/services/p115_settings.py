"""Read-only P115 settings and validation service."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
)

P115_COOKIE_SOURCE = "file"


class P115ValidationError(Exception):
    """Base class for stable, non-sensitive validation failures."""


class P115NeedsAuthError(P115ValidationError):
    """The local cookie is missing or rejected by 115."""


class P115UnavailableError(P115ValidationError):
    """The P115 client or read-only endpoint is unavailable."""


class P115ValidationAdapter(Protocol):
    async def validate_read_only(self) -> None:
        """Validate credentials using only the remote task-list endpoint."""


class P115ValidationRateLimited(Exception):
    """The validation endpoint exceeded its per-identity request limit."""


@dataclass(frozen=True)
class P115CookieSnapshot:
    source: str
    configured: bool
    structure_valid: bool


@dataclass(frozen=True)
class P115SettingsSnapshot:
    enabled: bool
    ready: bool
    capabilities: dict[str, bool]
    cookie: P115CookieSnapshot
    target_configured: bool
    max_concurrency: int


@dataclass(frozen=True)
class P115ValidationResult:
    status: str
    checked_at: datetime


class P115SettingsService:
    """Expose local P115 readiness and a bounded read-only validation probe."""

    def __init__(
        self,
        *,
        enabled: bool,
        cookie_provider: CookieProvider | CompositeCookieProvider,
        target_configured: bool,
        max_concurrency: int,
        adapter: P115ValidationAdapter | None = None,
        cookie_path: str | Path | None = None,
        validation_limit: int = 6,
        validation_window: timedelta = timedelta(minutes=1),
        validation_timeout_seconds: float = 10,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if validation_limit < 1:
            raise ValueError("validation_limit must be positive")
        if validation_window <= timedelta(0):
            raise ValueError("validation_window must be positive")
        if validation_timeout_seconds <= 0:
            raise ValueError("validation_timeout_seconds must be positive")
        self._enabled = enabled
        self._cookie_provider = cookie_provider
        self._cookie_path = Path(cookie_path) if cookie_path is not None else None
        self._target_configured = target_configured
        self._max_concurrency = max_concurrency
        self._adapter = adapter
        self._validation_limit = validation_limit
        self._validation_window = validation_window
        self._validation_timeout_seconds = validation_timeout_seconds
        self._validation_windows: dict[str, deque[datetime]] = {}

    def snapshot(
        self, *, runtime_ready: bool, runtime_magnet_capability: bool
    ) -> P115SettingsSnapshot:
        cookie = self._cookie_snapshot()
        ready = (
            self._enabled
            and self._target_configured
            and cookie.structure_valid
            and runtime_ready is True
        )
        return P115SettingsSnapshot(
            enabled=self._enabled,
            ready=ready,
            capabilities={
                "magnet": ready and runtime_magnet_capability is True,
                "share": False,
            },
            cookie=cookie,
            target_configured=self._target_configured,
            max_concurrency=self._max_concurrency,
        )

    def check_validation_rate_limit(
        self, identity: str, *, now: datetime | None = None
    ) -> None:
        checked_at = now or datetime.now(UTC)
        window = self._validation_windows.setdefault(identity, deque())
        cutoff = checked_at - self._validation_window
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._validation_limit:
            raise P115ValidationRateLimited
        window.append(checked_at)

    async def validate(self) -> P115ValidationResult:
        if not self._enabled or not self._target_configured:
            return self._validation_result("unavailable")
        if not self._cookie_snapshot().structure_valid:
            return self._validation_result("needs_auth")
        if self._adapter is None:
            return self._validation_result("unavailable")
        try:
            await asyncio.wait_for(
                self._adapter.validate_read_only(),
                timeout=self._validation_timeout_seconds,
            )
        except P115NeedsAuthError:
            return self._validation_result("needs_auth")
        except P115UnavailableError:
            return self._validation_result("unavailable")
        except Exception:  # noqa: BLE001 - validation must not leak adapter errors
            return self._validation_result("unavailable")
        return self._validation_result("ready")

    def _cookie_snapshot(self) -> P115CookieSnapshot:
        configured = False
        if self._cookie_path is not None:
            try:
                self._cookie_path.stat()
            except OSError:
                pass
            else:
                configured = True
        try:
            structure_valid = self._cookie_provider.load() is not None
        except Exception:  # noqa: BLE001 - local credential state fails closed
            structure_valid = False
        configured = configured or structure_valid
        source = (
            self._cookie_provider.source
            if isinstance(self._cookie_provider, CompositeCookieProvider)
            else P115_COOKIE_SOURCE
        )
        return P115CookieSnapshot(
            source=source,
            configured=configured,
            structure_valid=structure_valid,
        )

    @staticmethod
    def _validation_result(status: str) -> P115ValidationResult:
        return P115ValidationResult(status=status, checked_at=datetime.now(UTC))
