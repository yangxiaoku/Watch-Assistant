"""Small, in-memory health state machine for controlled search sources.

The tracker deliberately stores only bounded status data.  It never stores a
query, URL, header, response body, or credential.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class SourceHealthState(StrEnum):
    """Public readiness states for a configured search source."""

    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    UNVERIFIED = "unverified"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    BACKOFF = "backoff"
    OPEN_CIRCUIT = "open_circuit"


@dataclass(frozen=True, slots=True)
class SourceHealthSnapshot:
    state: SourceHealthState
    consecutive_failures: int
    total_successes: int
    total_failures: int
    last_error_code: str | None
    checked_at: datetime | None
    retry_at: datetime | None
    retry_after_seconds: int | None


class SourceHealthTracker:
    """Track one source without persisting sensitive request details."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        backoff_base_seconds: int = 15,
        backoff_max_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._failure_threshold = max(2, failure_threshold)
        self._backoff_base_seconds = max(1, backoff_base_seconds)
        self._backoff_max_seconds = max(
            self._backoff_base_seconds, backoff_max_seconds
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = SourceHealthState.UNVERIFIED
        self._consecutive_failures = 0
        self._total_successes = 0
        self._total_failures = 0
        self._last_error_code: str | None = None
        self._checked_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._probe_in_flight = False

    def reset(self, *, configured: bool = True) -> None:
        self._state = (
            SourceHealthState.UNVERIFIED
            if configured
            else SourceHealthState.NOT_CONFIGURED
        )
        self._consecutive_failures = 0
        self._last_error_code = None
        self._checked_at = None
        self._retry_at = None
        self._probe_in_flight = False

    def allow_request(self, *, now: datetime | None = None) -> bool:
        current = _as_utc(now or self._clock())
        if self._state not in {
            SourceHealthState.BACKOFF,
            SourceHealthState.OPEN_CIRCUIT,
        }:
            return True
        if self._probe_in_flight:
            return False
        if self._retry_at is not None and current < self._retry_at:
            return False
        # Permit exactly one half-open probe after the backoff window.
        self._probe_in_flight = True
        self._state = SourceHealthState.BACKOFF
        return True

    def record_success(self, *, now: datetime | None = None) -> None:
        self._state = SourceHealthState.AVAILABLE
        self._consecutive_failures = 0
        self._total_successes += 1
        self._last_error_code = None
        self._checked_at = _as_utc(now or self._clock())
        self._retry_at = None
        self._probe_in_flight = False

    def record_failure(
        self,
        error_code: str,
        *,
        now: datetime | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        current = _as_utc(now or self._clock())
        self._consecutive_failures += 1
        self._total_failures += 1
        self._last_error_code = _safe_code(error_code)
        self._checked_at = current
        self._probe_in_flight = False

        requested_delay = (
            retry_after_seconds
            if retry_after_seconds is not None and retry_after_seconds > 0
            else self._backoff_base_seconds
            * 2 ** max(0, self._consecutive_failures - 1)
        )
        delay = min(self._backoff_max_seconds, requested_delay)
        self._retry_at = current + timedelta(seconds=delay)
        if self._consecutive_failures >= self._failure_threshold:
            self._state = SourceHealthState.OPEN_CIRCUIT
        elif error_code == "prowlarr_rate_limited":
            self._state = SourceHealthState.BACKOFF
        else:
            self._state = SourceHealthState.DEGRADED

    def snapshot(self, *, now: datetime | None = None) -> SourceHealthSnapshot:
        current = _as_utc(now or self._clock())
        # A snapshot does not mutate state.  A caller can still see that the
        # circuit is ready for a probe through retry_after_seconds == 0.
        retry_after = (
            max(0, int((self._retry_at - current).total_seconds()))
            if self._retry_at is not None
            else None
        )
        return SourceHealthSnapshot(
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            total_successes=self._total_successes,
            total_failures=self._total_failures,
            last_error_code=self._last_error_code,
            checked_at=self._checked_at,
            retry_at=self._retry_at,
            retry_after_seconds=retry_after,
        )


def health_message(state: SourceHealthState) -> str:
    return {
        SourceHealthState.DISABLED: "Prowlarr 搜索来源未启用。",
        SourceHealthState.NOT_CONFIGURED: "Prowlarr 尚未配置有效的只读连接。",
        SourceHealthState.UNVERIFIED: "Prowlarr 配置已保存，尚未完成只读连接验证。",
        SourceHealthState.AVAILABLE: "Prowlarr 只读搜索可用。",
        SourceHealthState.DEGRADED: "Prowlarr 最近一次查询失败，已保留其他来源结果。",
        SourceHealthState.BACKOFF: "Prowlarr 暂时退避，系统将在稍后进行探测。",
        SourceHealthState.OPEN_CIRCUIT: "Prowlarr 连续失败，已暂时熔断，其他来源仍可用。",
    }[state]


def health_reason_code(
    state: SourceHealthState, last_error_code: str | None = None
) -> str:
    if state == SourceHealthState.DISABLED:
        return "prowlarr_disabled"
    if state == SourceHealthState.NOT_CONFIGURED:
        return "prowlarr_not_configured"
    if state == SourceHealthState.UNVERIFIED:
        return "prowlarr_unverified"
    if state == SourceHealthState.AVAILABLE:
        return "prowlarr_available"
    if state == SourceHealthState.OPEN_CIRCUIT:
        return "prowlarr_open_circuit"
    if last_error_code in {
        "prowlarr_auth_required",
        "prowlarr_invalid_response",
        "prowlarr_rate_limited",
        "prowlarr_server_error",
        "prowlarr_timeout",
    }:
        return last_error_code
    if state == SourceHealthState.BACKOFF:
        return "prowlarr_backoff"
    return "prowlarr_unavailable"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_code(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 80:
        return "source_unavailable"
    if not all(character.isalnum() or character in "_.-" for character in normalized):
        return "source_unavailable"
    return normalized
