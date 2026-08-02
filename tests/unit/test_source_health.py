from datetime import UTC, datetime, timedelta

from watch_assistant.services.source_health import (
    SourceHealthState,
    SourceHealthTracker,
    health_message,
    health_reason_code,
)


def test_source_health_transitions_from_unverified_to_available_and_degraded():
    current = [datetime(2026, 8, 2, tzinfo=UTC)]
    tracker = SourceHealthTracker(clock=lambda: current[0], backoff_base_seconds=10)

    assert tracker.snapshot().state == SourceHealthState.UNVERIFIED
    tracker.record_success()
    assert tracker.snapshot().state == SourceHealthState.AVAILABLE

    tracker.record_failure("prowlarr_timeout")
    snapshot = tracker.snapshot()
    assert snapshot.state == SourceHealthState.DEGRADED
    assert snapshot.last_error_code == "prowlarr_timeout"
    assert snapshot.consecutive_failures == 1
    assert snapshot.retry_after_seconds == 10


def test_rate_limit_backoff_allows_one_probe_after_retry_window():
    current = [datetime(2026, 8, 2, tzinfo=UTC)]
    tracker = SourceHealthTracker(clock=lambda: current[0], backoff_base_seconds=10)

    tracker.record_failure("prowlarr_rate_limited", retry_after_seconds=30)
    assert tracker.snapshot().state == SourceHealthState.BACKOFF
    assert tracker.allow_request() is False

    current[0] += timedelta(seconds=30)
    assert tracker.allow_request() is True
    assert tracker.allow_request() is False
    tracker.record_success()
    assert tracker.snapshot().state == SourceHealthState.AVAILABLE


def test_repeated_failures_open_circuit_without_sensitive_fields():
    tracker = SourceHealthTracker(
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
        backoff_base_seconds=10,
        failure_threshold=3,
    )
    for _ in range(3):
        tracker.record_failure("prowlarr_server_error")

    snapshot = tracker.snapshot()
    assert snapshot.state == SourceHealthState.OPEN_CIRCUIT
    assert snapshot.retry_after_seconds == 40
    assert not hasattr(snapshot, "query")
    assert not hasattr(snapshot, "api_key")


def test_health_reason_is_safe_and_chinese():
    reason = health_reason_code(
        SourceHealthState.BACKOFF, "prowlarr_rate_limited"
    )
    assert reason == "prowlarr_rate_limited"
    assert "Prowlarr" in health_message(SourceHealthState.BACKOFF)
    assert "api_key" not in health_message(SourceHealthState.BACKOFF)
