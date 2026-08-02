"""Read-only media-library health evidence and repair-plan gating."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HealthError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HealthSeverity(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RepairMode(StrEnum):
    NONE = "none"
    AUTO = "auto"
    CONFIRM = "confirm"


class TrendDirection(StrEnum):
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    object_id: str
    media_identity: str | None = None
    managed: bool = True

    def __post_init__(self) -> None:
        _validate_id(self.object_id, "object_id")
        if self.media_identity is not None:
            _validate_id(self.media_identity, "media_identity")


@dataclass(frozen=True, slots=True)
class StrmEvidence:
    manifest_id: str
    target_object_id: str | None
    target_present: bool
    content_valid: bool
    managed: bool = True

    def __post_init__(self) -> None:
        _validate_id(self.manifest_id, "manifest_id")
        if self.target_object_id is not None:
            _validate_id(self.target_object_id, "target_object_id")


@dataclass(frozen=True, slots=True)
class SubtitleEvidence:
    object_id: str
    required: bool
    present: bool
    managed: bool = True

    def __post_init__(self) -> None:
        _validate_id(self.object_id, "object_id")


@dataclass(frozen=True, slots=True)
class NamingEvidence:
    object_id: str
    compliant: bool
    expected_name: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.object_id, "object_id")
        if self.expected_name is not None and (
            not self.expected_name.strip() or len(self.expected_name) > 1024
        ):
            raise HealthError("invalid_expected_name")


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    task_id: str
    state: str

    def __post_init__(self) -> None:
        _validate_id(self.task_id, "task_id")
        if self.state not in {
            "queued",
            "submitting",
            "submitted",
            "downloading",
            "available",
            "needs_auth",
            "failed",
            "uncertain",
            "cancelled",
        }:
            raise HealthError("invalid_task_state")


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    snapshot_revision: int | None
    inventory_complete: bool
    inventory: tuple[InventoryEntry, ...] = ()
    strm: tuple[StrmEvidence, ...] = ()
    subtitles: tuple[SubtitleEvidence, ...] = ()
    naming: tuple[NamingEvidence, ...] = ()
    tasks: tuple[TaskEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class HealthIssue:
    issue_id: str
    check_code: str
    severity: HealthSeverity
    reason_code: str
    title_zh: str
    impact_zh: str
    suggestion_zh: str
    repair_mode: RepairMode
    object_ids: tuple[str, ...] = ()
    requires_complete_inventory: bool = False


@dataclass(frozen=True, slots=True)
class HealthReport:
    snapshot_revision: int | None
    inventory_complete: bool
    score: int
    module_scores: dict[str, int]
    issue_counts: dict[str, int]
    issues: tuple[HealthIssue, ...]


@dataclass(frozen=True, slots=True)
class RepairPlan:
    issue_ids: tuple[str, ...]
    reversible: bool
    inventory_complete: bool


@dataclass(frozen=True, slots=True)
class HealthTrend:
    previous_score: int
    current_score: int
    delta: int
    direction: TrendDirection


_SEVERITY_PENALTY = {
    HealthSeverity.CRITICAL: 40,
    HealthSeverity.ERROR: 20,
    HealthSeverity.WARNING: 5,
    HealthSeverity.INFO: 1,
}
_MODULES = ("inventory", "strm", "metadata", "subtitles", "tasks")


def build_health_report(snapshot: HealthSnapshot) -> HealthReport:
    """Build a report from captured evidence without reading or changing data."""

    _validate_unique_ids(snapshot)
    issues: list[HealthIssue] = []
    inventory_ids = {item.object_id for item in snapshot.inventory}

    for item in snapshot.strm:
        if item.target_object_id is None or not item.target_present:
            issues.append(
                _issue(
                    f"strm:orphan:{item.manifest_id}",
                    "CHK-001",
                    HealthSeverity.ERROR,
                    "orphan_manifest",
                    "STRM 清单缺少目标文件",
                    "播放入口可能已经失效。",
                    "核对目标文件和清单后重建受管 STRM。",
                    RepairMode.AUTO if item.managed else RepairMode.CONFIRM,
                    (item.manifest_id,),
                    requires_complete_inventory=True,
                )
            )
        elif item.target_object_id not in inventory_ids:
            issues.append(
                _issue(
                    f"strm:unknown-target:{item.manifest_id}",
                    "CHK-001",
                    HealthSeverity.WARNING,
                    "manifest_target_not_indexed",
                    "STRM 目标未出现在库存索引",
                    "当前无法确认播放目标是否仍然存在。",
                    "完成库存扫描后重新检查。",
                    RepairMode.NONE,
                    (item.manifest_id,),
                    requires_complete_inventory=True,
                )
            )
        if not item.content_valid:
            issues.append(
                _issue(
                    f"strm:invalid:{item.manifest_id}",
                    "CHK-004",
                    HealthSeverity.ERROR,
                    "invalid_strm",
                    "STRM 内容无效",
                    "媒体服务器可能无法解析该播放入口。",
                    "仅允许重建受管 STRM，非受管文件需要人工确认。",
                    RepairMode.AUTO if item.managed else RepairMode.CONFIRM,
                    (item.manifest_id,),
                )
            )

    for item in snapshot.subtitles:
        if item.required and not item.present:
            issues.append(
                _issue(
                    f"subtitle:missing:{item.object_id}",
                    "CHK-005",
                    HealthSeverity.WARNING,
                    "missing_required_subtitle",
                    "缺少所需字幕",
                    "当前媒体版本不满足字幕要求。",
                    "补充匹配字幕并在确认后重新检查。",
                    RepairMode.CONFIRM,
                    (item.object_id,),
                )
            )

    for item in snapshot.naming:
        if not item.compliant:
            issues.append(
                _issue(
                    f"naming:noncompliant:{item.object_id}",
                    "CHK-002",
                    HealthSeverity.WARNING,
                    "noncompliant_name",
                    "文件命名不规范",
                    "媒体检索、配对或整理可能出现歧义。",
                    "先生成命名预览，确认后再提交整理计划。",
                    RepairMode.CONFIRM,
                    (item.object_id,),
                    requires_complete_inventory=True,
                )
            )

    for item in snapshot.tasks:
        if item.state == "uncertain":
            issues.append(
                _issue(
                    f"task:uncertain:{item.task_id}",
                    "CHK-006",
                    HealthSeverity.ERROR,
                    "task_uncertain",
                    "任务结果待确认",
                    "远端状态不确定，重复提交可能造成重复任务。",
                    "先核对任务和远端状态，再决定是否重试。",
                    RepairMode.CONFIRM,
                    (item.task_id,),
                )
            )

    if not snapshot.inventory_complete:
        issues.append(
            _issue(
                "inventory:incomplete",
                "CHK-001",
                HealthSeverity.WARNING,
                "inventory_incomplete",
                "库存扫描未完成",
                "缺失和孤儿结论可能不完整。",
                "完成完整扫描后再生成需要库存确认的计划。",
                RepairMode.NONE,
            )
        )

    module_scores = _module_scores(issues)
    penalty = sum(_SEVERITY_PENALTY[item.severity] for item in issues)
    issue_counts = {
        severity.value: sum(item.severity == severity for item in issues)
        for severity in HealthSeverity
    }
    return HealthReport(
        snapshot_revision=snapshot.snapshot_revision,
        inventory_complete=snapshot.inventory_complete,
        score=max(0, 100 - penalty),
        module_scores=module_scores,
        issue_counts=issue_counts,
        issues=tuple(issues),
    )


def plan_repairs(snapshot: HealthSnapshot, issue_ids: tuple[str, ...]) -> RepairPlan:
    """Return a reviewable plan; execution is deliberately out of scope."""

    report = build_health_report(snapshot)
    selected = tuple(dict.fromkeys(issue_ids))
    known = {item.issue_id: item for item in report.issues}
    if any(issue_id not in known for issue_id in selected):
        raise HealthError("issue_not_found")
    selected_issues = tuple(known[issue_id] for issue_id in selected)
    if any(item.repair_mode == RepairMode.NONE for item in selected_issues):
        raise HealthError("issue_not_repairable")
    if any(
        item.requires_complete_inventory
        for item in selected_issues
    ) and not report.inventory_complete:
        raise HealthError("inventory_incomplete")
    if any(item.repair_mode != RepairMode.AUTO for item in selected_issues):
        raise HealthError("confirmation_required")
    return RepairPlan(
        issue_ids=selected,
        reversible=True,
        inventory_complete=report.inventory_complete,
    )


def compare_health(previous: HealthReport, current: HealthReport) -> HealthTrend:
    delta = current.score - previous.score
    direction = (
        TrendDirection.IMPROVED
        if delta > 0
        else TrendDirection.REGRESSED
        if delta < 0
        else TrendDirection.UNCHANGED
    )
    return HealthTrend(previous.score, current.score, delta, direction)


def _issue(
    issue_id: str,
    check_code: str,
    severity: HealthSeverity,
    reason_code: str,
    title_zh: str,
    impact_zh: str,
    suggestion_zh: str,
    repair_mode: RepairMode,
    object_ids: tuple[str, ...] = (),
    *,
    requires_complete_inventory: bool = False,
) -> HealthIssue:
    return HealthIssue(
        issue_id=issue_id,
        check_code=check_code,
        severity=severity,
        reason_code=reason_code,
        title_zh=title_zh,
        impact_zh=impact_zh,
        suggestion_zh=suggestion_zh,
        repair_mode=repair_mode,
        object_ids=object_ids,
        requires_complete_inventory=requires_complete_inventory,
    )


def _module_scores(issues: list[HealthIssue]) -> dict[str, int]:
    scores = {module: 100 for module in _MODULES}
    for issue in issues:
        module = {
            "CHK-001": "inventory",
            "CHK-002": "metadata",
            "CHK-004": "strm",
            "CHK-005": "subtitles",
            "CHK-006": "tasks",
        }.get(issue.check_code)
        if module is not None:
            scores[module] = max(0, scores[module] - _SEVERITY_PENALTY[issue.severity])
    return scores


def _validate_unique_ids(snapshot: HealthSnapshot) -> None:
    if len({item.object_id for item in snapshot.inventory}) != len(snapshot.inventory):
        raise HealthError("duplicate_inventory_object")
    if len({item.manifest_id for item in snapshot.strm}) != len(snapshot.strm):
        raise HealthError("duplicate_manifest")
    if len({item.object_id for item in snapshot.subtitles}) != len(snapshot.subtitles):
        raise HealthError("duplicate_subtitle_object")
    if len({item.object_id for item in snapshot.naming}) != len(snapshot.naming):
        raise HealthError("duplicate_naming_object")
    if len({item.task_id for item in snapshot.tasks}) != len(snapshot.tasks):
        raise HealthError("duplicate_task")


def _validate_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or "\x00" in value:
        raise HealthError(f"invalid_{field}")


__all__ = [
    "HealthError",
    "HealthIssue",
    "HealthReport",
    "HealthSeverity",
    "HealthSnapshot",
    "HealthTrend",
    "InventoryEntry",
    "NamingEvidence",
    "RepairMode",
    "RepairPlan",
    "StrmEvidence",
    "SubtitleEvidence",
    "TaskEvidence",
    "TrendDirection",
    "build_health_report",
    "compare_health",
    "plan_repairs",
]
