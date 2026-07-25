"""Read-only P115 settings and validation service."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from watch_assistant.services.p115_credentials import CookieProvider

P115_COOKIE_SOURCE = "tgtodrive"
COOKIE_SYNC_STATUSES = frozenset(("success", "failed", "unknown"))


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
    sync_status: str
    last_sync_at: datetime | None


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
        cookie_provider: CookieProvider,
        target_configured: bool,
        max_concurrency: int,
        adapter: P115ValidationAdapter | None = None,
        cookie_path: str | Path | None = None,
        sync_status: str | None = None,
        last_sync_at: datetime | None = None,
        validation_limit: int = 6,
        validation_window: timedelta = timedelta(minutes=1),
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if validation_limit < 1:
            raise ValueError("validation_limit must be positive")
        if validation_window <= timedelta(0):
            raise ValueError("validation_window must be positive")
        if sync_status is not None and sync_status not in COOKIE_SYNC_STATUSES:
            raise ValueError("sync_status is invalid")
        self._enabled = enabled
        self._cookie_provider = cookie_provider
        self._cookie_path = Path(cookie_path) if cookie_path is not None else None
        self._target_configured = target_configured
        self._max_concurrency = max_concurrency
        self._adapter = adapter
        self._sync_status = sync_status
        self._last_sync_at = last_sync_at
        self._validation_limit = validation_limit
        self._validation_window = validation_window
        self._validation_windows: dict[str, deque[datetime]] = {}

    def snapshot(self) -> P115SettingsSnapshot:
        cookie = self._cookie_snapshot()
        ready = self._enabled and self._target_configured and cookie.structure_valid
        return P115SettingsSnapshot(
            enabled=self._enabled,
            ready=ready,
            capabilities={"magnet": ready, "share": False},
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
            await self._adapter.validate_read_only()
        except P115NeedsAuthError:
            return self._validation_result("needs_auth")
        except P115UnavailableError:
            return self._validation_result("unavailable")
        except Exception:  # noqa: BLE001 - validation must not leak adapter errors
            return self._validation_result("unavailable")
        return self._validation_result("ready")

    def _cookie_snapshot(self) -> P115CookieSnapshot:
        configured = False
        modified_at: datetime | None = None
        if self._cookie_path is not None:
            try:
                file_stat = self._cookie_path.stat()
            except OSError:
                pass
            else:
                configured = True
                modified_at = datetime.fromtimestamp(file_stat.st_mtime, UTC)
        try:
            structure_valid = self._cookie_provider.load() is not None
        except Exception:  # noqa: BLE001 - local credential state fails closed
            structure_valid = False
        configured = configured or structure_valid
        if self._sync_status is not None:
            sync_status = self._sync_status
        elif not configured:
            sync_status = "unknown"
        else:
            sync_status = "success" if structure_valid else "failed"
        return P115CookieSnapshot(
            source=P115_COOKIE_SOURCE,
            configured=configured,
            structure_valid=structure_valid,
            sync_status=sync_status,
            last_sync_at=self._last_sync_at or modified_at,
        )

    @staticmethod
    def _validation_result(status: str) -> P115ValidationResult:
        return P115ValidationResult(status=status, checked_at=datetime.now(UTC))
