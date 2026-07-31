"""Build and persist read-only media-library health reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryHealthReport,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
)
from watch_assistant.models import Task, TaskState
from watch_assistant.services.library_health import (
    HealthIssue,
    HealthReport,
    HealthSeverity,
    HealthSnapshot,
    HealthTrend,
    InventoryEntry,
    RepairMode,
    StrmEvidence,
    TaskEvidence,
    TrendDirection,
    build_health_report,
    compare_health,
)
from watch_assistant.services.observability import EventLogger, emit_event


class LibraryHealthServiceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HealthRun:
    report_id: str
    library_id: str
    source_scan_run_id: str
    created_at: datetime
    report: HealthReport
    trend: HealthTrend | None


class LibraryHealthService:
    """Evaluate only persisted local evidence; never reads or mutates 115."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def run(self, library_id: str, *, scan_run_id: str | None = None) -> HealthRun:
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, library_id)
            if library is None:
                raise LibraryHealthServiceError("library_not_found")
            run = await self._scan_run(session, library_id, scan_run_id)
            if run is None:
                raise LibraryHealthServiceError("library_health_scan_unavailable")

            existing = await session.scalar(
                select(LibraryHealthReport).where(
                    LibraryHealthReport.source_scan_run_id == run.id
                )
            )
            if existing is not None:
                return _run_from_row(existing)

            report = await self._build_report(session, library, run)
            previous_row = await session.scalar(
                select(LibraryHealthReport)
                .where(LibraryHealthReport.library_id == library_id)
                .order_by(LibraryHealthReport.created_at.desc())
                .limit(1)
            )
            previous = None if previous_row is None else _report_from_row(previous_row)
            trend = None if previous is None else compare_health(previous, report)
            row = LibraryHealthReport(
                id="health_" + uuid4().hex,
                library_id=library_id,
                source_scan_run_id=run.id,
                source_snapshot_revision=report.snapshot_revision,
                inventory_complete=report.inventory_complete,
                score=report.score,
                module_scores_json=json.dumps(report.module_scores, sort_keys=True),
                issue_counts_json=json.dumps(report.issue_counts, sort_keys=True),
                issues_json=json.dumps(
                    [_issue_dict(issue) for issue in report.issues],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                previous_score=None if trend is None else trend.previous_score,
                trend_direction=(
                    TrendDirection.UNCHANGED.value if trend is None else trend.direction.value
                ),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent request may have won the source-scan unique key.
                await session.rollback()
                existing = await session.scalar(
                    select(LibraryHealthReport).where(
                        LibraryHealthReport.source_scan_run_id == run.id
                    )
                )
                if existing is None:
                    raise
                return _run_from_row(existing)
            await session.refresh(row)
            result = _run_from_row(row)

        severe_count = sum(
            issue.severity in {HealthSeverity.CRITICAL, HealthSeverity.ERROR}
            and (report.inventory_complete or not issue.requires_complete_inventory)
            for issue in report.issues
        )
        if severe_count:
            await emit_event(
                self._event_logger,
                "library.health.critical",
                fields={"status": "critical", "count": severe_count},
                correlation_id=result.source_scan_run_id,
                resource_type="library",
                resource_id=library_id,
            )
        return result

    async def latest(self, library_id: str) -> HealthRun:
        async with self._session_factory() as session:
            if await session.get(MediaLibrary, library_id) is None:
                raise LibraryHealthServiceError("library_not_found")
            row = await session.scalar(
                select(LibraryHealthReport)
                .where(LibraryHealthReport.library_id == library_id)
                .order_by(LibraryHealthReport.created_at.desc())
                .limit(1)
            )
            if row is None:
                raise LibraryHealthServiceError("library_health_report_not_found")
            return _run_from_row(row)

    async def _build_report(
        self,
        session: AsyncSession,
        library: MediaLibrary,
        run: LibraryScanRun,
    ) -> HealthReport:
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id,
                        LibraryScanEntry.is_directory.is_(False),
                    )
                )
            ).all()
        )
        inventory_ids = {entry.object_id for entry in entries}
        manifests = list(
            (
                await session.scalars(
                    select(StrmManifestEntry).where(
                        StrmManifestEntry.library_id == library.id,
                        StrmManifestEntry.is_current.is_(True),
                    )
                )
            ).all()
        )
        tasks = list(
            (
                await session.scalars(
                    select(Task).where(
                        Task.target_directory_id == library.root_directory_id
                    )
                )
            ).all()
        )
        task_states = {
            TaskState.QUEUED.value,
            TaskState.SUBMITTING.value,
            TaskState.ACCEPTED.value,
            TaskState.NEEDS_AUTH.value,
            TaskState.FAILED.value,
            TaskState.UNCERTAIN.value,
        }
        snapshot = HealthSnapshot(
            snapshot_revision=run.snapshot_revision,
            inventory_complete=run.complete and run.state == "completed",
            inventory=tuple(InventoryEntry(entry.object_id) for entry in entries),
            strm=tuple(
                StrmEvidence(
                    manifest_id=manifest.manifest_id,
                    target_object_id=manifest.cloud_file_id,
                    target_present=manifest.cloud_file_id in inventory_ids,
                    content_valid=manifest.status == "verified",
                )
                for manifest in manifests
            ),
            tasks=tuple(
                TaskEvidence(
                    task_id=task.id,
                    state=(task.state.value if hasattr(task.state, "value") else str(task.state)),
                )
                for task in tasks
                if (task.state.value if hasattr(task.state, "value") else str(task.state))
                in task_states
            ),
        )
        return build_health_report(snapshot)

    @staticmethod
    async def _scan_run(
        session: AsyncSession,
        library_id: str,
        scan_run_id: str | None,
    ) -> LibraryScanRun | None:
        if scan_run_id is not None:
            run = await session.get(LibraryScanRun, scan_run_id)
            if run is None or run.library_id != library_id:
                raise LibraryHealthServiceError("library_health_scan_not_found")
            return run
        return await session.scalar(
            select(LibraryScanRun)
            .where(LibraryScanRun.library_id == library_id)
            .order_by(LibraryScanRun.updated_at.desc())
            .limit(1)
        )


def _issue_dict(issue: HealthIssue) -> dict[str, object]:
    return {
        "issue_id": issue.issue_id,
        "check_code": issue.check_code,
        "severity": issue.severity.value,
        "reason_code": issue.reason_code,
        "title_zh": issue.title_zh,
        "impact_zh": issue.impact_zh,
        "suggestion_zh": issue.suggestion_zh,
        "repair_mode": issue.repair_mode.value,
        "object_ids": list(issue.object_ids),
        "requires_complete_inventory": issue.requires_complete_inventory,
    }


def _issue_from_dict(value: object) -> HealthIssue:
    if not isinstance(value, dict):
        raise LibraryHealthServiceError("library_health_report_corrupt")
    try:
        return HealthIssue(
            issue_id=str(value["issue_id"]),
            check_code=str(value["check_code"]),
            severity=HealthSeverity(str(value["severity"])),
            reason_code=str(value["reason_code"]),
            title_zh=str(value["title_zh"]),
            impact_zh=str(value["impact_zh"]),
            suggestion_zh=str(value["suggestion_zh"]),
            repair_mode=RepairMode(str(value["repair_mode"])),
            object_ids=tuple(str(item) for item in value.get("object_ids", [])),
            requires_complete_inventory=bool(value.get("requires_complete_inventory", False)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LibraryHealthServiceError("library_health_report_corrupt") from error


def _report_from_row(row: LibraryHealthReport) -> HealthReport:
    try:
        module_scores = json.loads(row.module_scores_json)
        issue_counts = json.loads(row.issue_counts_json)
        issues = tuple(_issue_from_dict(item) for item in json.loads(row.issues_json))
        if not isinstance(module_scores, dict) or not isinstance(issue_counts, dict):
            raise TypeError
        return HealthReport(
            snapshot_revision=row.source_snapshot_revision,
            inventory_complete=row.inventory_complete,
            score=row.score,
            module_scores={str(key): int(value) for key, value in module_scores.items()},
            issue_counts={str(key): int(value) for key, value in issue_counts.items()},
            issues=issues,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LibraryHealthServiceError("library_health_report_corrupt") from error


def _run_from_row(row: LibraryHealthReport) -> HealthRun:
    report = _report_from_row(row)
    trend = None
    if row.previous_score is not None:
        current = row.score
        trend = HealthTrend(
            previous_score=row.previous_score,
            current_score=current,
            delta=current - row.previous_score,
            direction=TrendDirection(row.trend_direction),
        )
    return HealthRun(
        report_id=row.id,
        library_id=row.library_id,
        source_scan_run_id=row.source_scan_run_id,
        created_at=(
            row.created_at
            if row.created_at.tzinfo is not None
            else row.created_at.replace(tzinfo=UTC)
        ),
        report=report,
        trend=trend,
    )


__all__ = [
    "HealthRun",
    "LibraryHealthService",
    "LibraryHealthServiceError",
]
