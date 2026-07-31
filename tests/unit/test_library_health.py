import pytest

from watch_assistant.services.episode_completeness import (
    EpisodeBaseline,
    EpisodeFileReference,
    EpisodeMatrixStatus,
    build_episode_matrix,
)
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


def test_report_includes_episode_completeness_issues_without_auto_repair():
    matrix = build_episode_matrix(
        (
            EpisodeBaseline(1, aired=True),
            EpisodeBaseline(2, aired=True),
            EpisodeBaseline(3, aired=True),
        ),
        (
            EpisodeFileReference("file-a", (1,)),
            EpisodeFileReference("file-b", (1,)),
        ),
        inventory_complete=True,
    )
    report = build_health_report(
        HealthSnapshot(7, True, episode_matrix=matrix)
    )

    issues = {item.reason_code: item for item in report.issues}
    assert matrix.items[0].status is EpisodeMatrixStatus.MULTIPLE
    assert {"multiple_episode_versions", "missing_episode"} <= issues.keys()
    assert issues["missing_episode"].repair_mode == RepairMode.NONE
    assert issues["multiple_episode_versions"].repair_mode == RepairMode.CONFIRM
    with pytest.raises(HealthError, match="issue_not_repairable"):
        plan_repairs(HealthSnapshot(7, True, episode_matrix=matrix), (issues["missing_episode"].issue_id,))
    with pytest.raises(HealthError, match="confirmation_required"):
        plan_repairs(
            HealthSnapshot(7, True, episode_matrix=matrix),
            (issues["multiple_episode_versions"].issue_id,),
        )


def test_report_keeps_unrecognized_episode_files_as_read_only_notice():
    matrix = build_episode_matrix(
        (EpisodeBaseline(1, aired=True),),
        (EpisodeFileReference("file-unmapped", (), recognized=False),),
        inventory_complete=True,
    )

    report = build_health_report(HealthSnapshot(9, True, episode_matrix=matrix))

    issue = next(
        item for item in report.issues if item.reason_code == "unrecognized_episode_files"
    )
    assert issue.check_code == "CHK-003"
    assert issue.repair_mode == RepairMode.NONE
    assert issue.object_ids == ("file-unmapped",)


def test_incomplete_episode_matrix_reports_unknown_instead_of_missing():
    matrix = build_episode_matrix(
        (EpisodeBaseline(1, aired=True),),
        (EpisodeFileReference("file-special", (), special=True),),
        inventory_complete=False,
    )
    report = build_health_report(
        HealthSnapshot(8, False, episode_matrix=matrix)
    )

    assert any(item.reason_code == "episode_unknown" for item in report.issues)
    assert not any(item.reason_code == "missing_episode" for item in report.issues)
