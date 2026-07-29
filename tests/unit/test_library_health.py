import pytest

from watch_assistant.services.library_health import (
    HealthError,
    HealthSeverity,
    HealthSnapshot,
    InventoryEntry,
    NamingEvidence,
    RepairMode,
    StrmEvidence,
    SubtitleEvidence,
    TaskEvidence,
    TrendDirection,
    build_health_report,
    compare_health,
    plan_repairs,
)


def test_report_finds_required_health_checks_and_explains_each_issue():
    report = build_health_report(
        HealthSnapshot(
            snapshot_revision=4,
            inventory_complete=True,
            inventory=(InventoryEntry("movie-1"),),
            strm=(
                StrmEvidence("strm-1", "movie-1", target_present=True, content_valid=False),
                StrmEvidence("strm-2", None, target_present=False, content_valid=True),
            ),
            subtitles=(SubtitleEvidence("movie-1", required=True, present=False),),
            naming=(NamingEvidence("movie-1", compliant=False, expected_name="Movie.mkv"),),
            tasks=(TaskEvidence("task-1", "uncertain"),),
        )
    )

    assert report.score == 30
    assert report.issue_counts == {"critical": 0, "error": 3, "warning": 2, "info": 0}
    assert {item.reason_code for item in report.issues} == {
        "invalid_strm",
        "orphan_manifest",
        "missing_required_subtitle",
        "noncompliant_name",
        "task_uncertain",
    }
    assert all(item.title_zh and item.impact_zh and item.suggestion_zh for item in report.issues)


def test_incomplete_inventory_never_allows_inventory_dependent_repair():
    snapshot = HealthSnapshot(
        snapshot_revision=None,
        inventory_complete=False,
        strm=(StrmEvidence("strm-1", None, target_present=False, content_valid=True),),
    )
    report = build_health_report(snapshot)
    orphan_id = next(item.issue_id for item in report.issues if item.reason_code == "orphan_manifest")

    with pytest.raises(HealthError, match="inventory_incomplete"):
        plan_repairs(snapshot, (orphan_id,))


def test_non_managed_strm_requires_confirmation_and_is_never_auto_planned():
    snapshot = HealthSnapshot(
        snapshot_revision=1,
        inventory_complete=True,
        inventory=(InventoryEntry("movie-1"),),
        strm=(StrmEvidence("strm-1", "movie-1", target_present=True, content_valid=False, managed=False),),
    )
    report = build_health_report(snapshot)
    issue = next(item for item in report.issues if item.reason_code == "invalid_strm")

    assert issue.repair_mode == RepairMode.CONFIRM
    with pytest.raises(HealthError, match="confirmation_required"):
        plan_repairs(snapshot, (issue.issue_id,))


def test_managed_invalid_strm_has_only_reversible_auto_plan():
    snapshot = HealthSnapshot(
        snapshot_revision=2,
        inventory_complete=True,
        inventory=(InventoryEntry("movie-1"),),
        strm=(StrmEvidence("strm-1", "movie-1", target_present=True, content_valid=False),),
    )
    report = build_health_report(snapshot)
    issue = next(item for item in report.issues if item.reason_code == "invalid_strm")

    plan = plan_repairs(snapshot, (issue.issue_id,))

    assert plan.reversible is True
    assert plan.inventory_complete is True


def test_health_trend_is_deterministic():
    before = build_health_report(HealthSnapshot(1, True))
    after = build_health_report(
        HealthSnapshot(2, True, naming=(NamingEvidence("movie-1", compliant=False),))
    )

    trend = compare_health(before, after)

    assert trend.delta < 0
    assert trend.direction == TrendDirection.REGRESSED
    assert HealthSeverity.WARNING.value in after.issue_counts
