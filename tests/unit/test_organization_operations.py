import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    OrganizationOperation,
    OrganizationOperationStatus,
    Resource,
    Task,
)
from watch_assistant.schemas import (
    MediaType,
    RemoteObservation,
    RemoteStatus,
    WorkflowCreateRequest,
    WorkflowStageName,
    WorkflowStagePatch,
    WorkflowStageStatus,
)
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingPlan,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchStatus,
    MediaKind,
    TmdbCandidate,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationConflict,
    OrganizationOperationLeaseUnavailable,
    OrganizationOperationPrerequisiteError,
    OrganizationOperationService,
    OrganizationOperationStateError,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanItem,
    OrganizationPlanService,
    OrganizationPlanStatus,
    PlanSource,
    _entry_remote_version,
)
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.workflows import WorkflowService

LIBRARY_ID = "library-1"
ROOT_ID = "7000"
SCAN_ID = "scan-1"


def _source_version() -> str:
    return _entry_remote_version(
        LibraryScanEntry(
            scan_run_id=SCAN_ID,
            object_type="file",
            object_id="100",
            parent_id=ROOT_ID,
            name="movie.mkv",
            path="/private/movie.mkv",
            is_directory=False,
        )
    )


def _item(
    *, confidence: MatchConfidence = MatchConfidence.HIGH
) -> OrganizationPlanItem:
    return OrganizationPlanItem(
        source=PlanSource(
            object_type="file",
            object_id="100",
            parent_id=ROOT_ID,
            path="/private/movie.mkv",
            remote_version=_source_version(),
        ),
        naming_plan=NamingPlan(
            status=ClassificationStatus.PLANNED,
            target_path="movie/movie.mkv",
            display_name="Movie",
            reasons=("accepted",),
            rule_version="i06-v1",
        ),
        decision=MatchDecision(
            status=MatchStatus.ACCEPTED,
            selected=TmdbCandidate(
                tmdb_id=1,
                media_type=MediaType.MOVIE,
                title="Movie",
                kind=MediaKind.MOVIE,
                release_year=2024,
                origin_countries=("US",),
            ),
            confidence=confidence,
        ),
        target_parent_id="8000",
        target_name="movie.mkv",
    )


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="workflow-resource",
                kind="magnet",
                canonical_key="magnet:workflow-resource",
                encrypted_url="encrypted-workflow-resource",
                name="Workflow resource",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="local",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id=SCAN_ID,
                library_id=LIBRARY_ID,
                root_directory_id=ROOT_ID,
                idempotency_key="scan-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=2,
                pages_read=2,
                items_seen=2,
            )
        )
        await session.commit()
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="directory",
                object_id="8000",
                parent_id=ROOT_ID,
                name="movie",
                path="movie",
                is_directory=True,
            )
        )
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="file",
                object_id="100",
                parent_id=ROOT_ID,
                name="movie.mkv",
                path="/private/movie.mkv",
                is_directory=False,
            )
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id=SCAN_ID,
                page=2,
                items_seen=2,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {ROOT_ID: 2, "8000": 0},
                        "expected_total": 2,
                        "pending": [],
                        "visited": [ROOT_ID, "8000"],
                    }
                ),
            )
        )
        await session.commit()
    return database


async def _refresh_completed_tree_evidence(database):
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, SCAN_ID)
        checkpoint = await session.get(LibraryScanCheckpoint, SCAN_ID)
        assert run is not None
        assert checkpoint is not None
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == SCAN_ID
                    )
                )
            ).all()
        )
        directory_ids = [run.root_directory_id]
        directory_totals = {run.root_directory_id: 0}
        for entry in entries:
            directory_totals[entry.parent_id] = (
                directory_totals.get(entry.parent_id, 0) + 1
            )
            if entry.is_directory:
                directory_ids.append(entry.object_id)
                directory_totals.setdefault(entry.object_id, 0)
        assert set(directory_totals) == set(directory_ids)
        run.expected_total = len(entries)
        run.items_seen = len(entries)
        checkpoint.items_seen = len(entries)
        checkpoint.cursor_json = json.dumps(
            {
                "version": 2,
                "directory_totals": directory_totals,
                "expected_total": len(entries),
                "pending": [],
                "visited": directory_ids,
            }
        )
        await session.commit()


async def _plan(database, *, confidence: MatchConfidence = MatchConfidence.HIGH):
    return await OrganizationPlanService(database.session_factory).create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(confidence=confidence),),
    )


async def _operation(database, *, key: str = "operation-1"):
    plan = await _plan(database)
    return await OrganizationOperationService(database.session_factory).create(
        plan.plan_id, idempotency_key=key
    )


@pytest.mark.asyncio
async def test_organization_operation_updates_linked_workflow_stage(tmp_path):
    database = await _database(tmp_path)
    workflow = await WorkflowService(database.session_factory).create(
        WorkflowCreateRequest(
            media_type=MediaType.MOVIE, tmdb_id=1, resource_id="workflow-resource"
        )
    )
    plan = await _plan(database)
    service = OrganizationOperationService(database.session_factory)
    operation = await service.create(
        plan.plan_id,
        idempotency_key="workflow-organization",
        workflow_id=workflow.id,
    )
    assert operation.workflow_id == workflow.id

    queued = await WorkflowService(database.session_factory).get(workflow.id)
    organization_stage = next(
        stage for stage in queued.stages if stage.stage is WorkflowStageName.ORGANIZATION
    )
    assert organization_stage.status is WorkflowStageStatus.PENDING
    assert organization_stage.child_type == "organization_operation"
    assert organization_stage.child_id == operation.operation_id

    workflow_service = WorkflowService(database.session_factory)
    for stage_name in (WorkflowStageName.INSPECTION, WorkflowStageName.APPROVAL):
        await workflow_service.patch_stage(
            workflow.id,
            stage_name,
            WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
        )
    task, _ = await TaskService(database.session_factory).create(
        "workflow-resource", workflow_id=workflow.id
    )
    async with database.session_factory() as session:
        stored_task = await session.get(Task, task.id)
        assert stored_task is not None
        stored_task.remote_ref = "workflow-available"
        await session.commit()

    class AvailableAdapter:
        async def get_status_for_task(
            self, remote_ref: str, *, target_directory_id: str | None
        ):
            assert remote_ref == "workflow-available"
            return RemoteObservation(
                status=RemoteStatus.AVAILABLE,
                file_id="101",
                parent_id="7",
                is_directory=False,
            )

    await TaskService(database.session_factory).reconcile(task.id, AvailableAdapter())

    lease = await service.claim(operation.operation_id, expected_revision=1)
    running = await WorkflowService(database.session_factory).get(workflow.id)
    organization_stage = next(
        stage
        for stage in running.stages
        if stage.stage is WorkflowStageName.ORGANIZATION
    )
    assert organization_stage.status is WorkflowStageStatus.RUNNING

    finished = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code="local_failure",
    )
    assert finished.status is OrganizationOperationStatus.FAILED
    failed = await WorkflowService(database.session_factory).get(workflow.id)
    organization_stage = next(
        stage
        for stage in failed.stages
        if stage.stage is WorkflowStageName.ORGANIZATION
    )
    assert organization_stage.status is WorkflowStageStatus.FAILED
    assert organization_stage.error_code == "local_failure"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_prerequisite_failure_invalidates_plan_and_can_be_queried_by_plan(
    tmp_path,
):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="stale-plan-operation")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    finished = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code="plan_prerequisites_changed",
    )

    assert finished.error_code == "plan_prerequisites_changed"
    assert (await service.get_for_plan(operation.plan_id)).operation_id == operation.operation_id
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        assert plan.status == OrganizationPlanStatus.INVALIDATED.value
        assert plan.revision == 2
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_finish_target_root_changed_keeps_plan_planned(tmp_path):
    # 目标根不一致是配置变更而非计划失效:操作 failed 但计划保持 planned。
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="target-root-changed")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    finished = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code="target_root_changed",
    )

    assert finished.status is OrganizationOperationStatus.FAILED
    assert finished.error_code == "target_root_changed"
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        assert plan.status == OrganizationPlanStatus.PLANNED.value
        assert plan.revision == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_finish_accepts_cleanup_postcondition_mismatch(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="cleanup-postcondition")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    finished = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.UNCERTAIN,
        error_code="cleanup_postcondition_mismatch",
    )

    assert finished.status is OrganizationOperationStatus.UNCERTAIN
    assert finished.error_code == "cleanup_postcondition_mismatch"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_needs_review_without_creating_operation(tmp_path):
    database = await _database(tmp_path)
    plan = await _plan(database, confidence=MatchConfidence.LOW)
    service = OrganizationOperationService(database.session_factory)

    with pytest.raises(OrganizationOperationPrerequisiteError):
        await service.create(plan.plan_id, idempotency_key="needs-review")

    async with database.session_factory() as session:
        assert await session.scalar(select(OrganizationOperation)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_running_operation_cancel_is_durable_and_idempotent(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="running-cancel")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    requested = await service.cancel(
        operation.operation_id, expected_revision=lease.revision
    )
    assert requested.status is OrganizationOperationStatus.ORGANIZING
    assert requested.cancel_requested is True
    assert await service.cancel_requested(operation.operation_id) is True

    repeated = await service.cancel(
        operation.operation_id, expected_revision=lease.revision
    )
    assert repeated.cancel_requested is True
    assert repeated.revision == lease.revision
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_planned_review_only_plan(tmp_path):
    database = await _database(tmp_path)
    plan = await _plan(database)
    async with database.session_factory() as session:
        row = await session.get(OrganizationPlan, plan.plan_id)
        assert row is not None
        row.actions_json = json.dumps([{"kind": "review"}])
        await session.commit()

    with pytest.raises(OrganizationOperationPrerequisiteError, match="plan_not_executable"):
        await OrganizationOperationService(database.session_factory).create(
            plan.plan_id, idempotency_key="review-only"
        )
    async with database.session_factory() as session:
        assert await session.scalar(select(OrganizationOperation)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_claim_next_finishes_legacy_non_executable_operation(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="legacy-operation")
    async with database.session_factory() as session:
        row = await session.get(OrganizationPlan, operation.plan_id)
        assert row is not None
        row.actions_json = json.dumps([{"kind": "review"}])
        await session.commit()

    assert await service.claim_next() is None
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.FAILED
    assert current.error_code == "plan_not_executable"
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ("write_disabled", "scope_unverified", "approval_required"),
)
async def test_finish_persists_worker_gate_failures_as_failed(tmp_path, error_code):
    case_dir = tmp_path / error_code
    case_dir.mkdir()
    database = await _database(case_dir)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key=f"gate-{error_code}")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    finished = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code=error_code,
    )

    assert finished.status is OrganizationOperationStatus.FAILED
    assert finished.error_code == error_code
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_claim_next_marks_expired_organizing_operation_uncertain(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="expired-organizing")
    first = await service.claim(
        operation.operation_id,
        expected_revision=1,
        lease_duration=timedelta(seconds=1),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    recovered = await service.claim_next(now=datetime(2026, 7, 28, 0, 0, 2, tzinfo=UTC))
    assert recovered is None
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.revision == first.revision + 1
    assert current.attempts == 1
    assert current.error_code == "outcome_unknown"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_claim_rejects_non_planned_plan_without_lease(tmp_path):
    database = await _database(tmp_path)
    plan = await _plan(database)
    service = OrganizationOperationService(database.session_factory)
    operation = await service.create(plan.plan_id, idempotency_key="planned")

    async with database.session_factory() as session:
        row = await session.get(
            OrganizationPlan,
            plan.plan_id,
        )
        assert row is not None
        row.status = OrganizationPlanStatus.IGNORED.value
        await session.commit()

    with pytest.raises(OrganizationOperationPrerequisiteError):
        await service.claim(operation.operation_id, expected_revision=1)
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.PLANNED
    assert current.revision == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_scan_rejects_create_without_operation(tmp_path):
    database = await _database(tmp_path)
    plan = await _plan(database)
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, SCAN_ID)
        assert run is not None
        run.complete = False
        await session.commit()
    service = OrganizationOperationService(database.session_factory)

    with pytest.raises(OrganizationOperationPrerequisiteError):
        await service.create(plan.plan_id, idempotency_key="incomplete")
    async with database.session_factory() as session:
        assert await session.scalar(select(OrganizationOperation)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "complete", "snapshot_revision"),
    # completed 用例用 revision=2 构造"非当前快照"(歧义已由迁移 072 杜绝)
    (("completed", True, 2), ("queued", False, None)),
)
async def test_claim_rejects_ambiguous_or_unsettled_source_scan(
    tmp_path, state: str, complete: bool, snapshot_revision: int | None
):
    database = await _database(tmp_path)
    operation = await _operation(database, key=f"scan-gate-{state}")
    async with database.session_factory() as session:
        now = datetime.now(UTC)
        created_at = now - timedelta(days=1) if state == "queued" else now
        updated_at = now + timedelta(seconds=1)
        session.add(
            LibraryScanRun(
                id=f"scan-{state}",
                library_id=LIBRARY_ID,
                root_directory_id=ROOT_ID,
                idempotency_key=f"scan-{state}-key",
                scan_mode="tree",
                state=state,
                complete=complete,
                snapshot_revision=snapshot_revision,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        await session.commit()

    service = OrganizationOperationService(database.session_factory)
    with pytest.raises(
        OrganizationOperationPrerequisiteError, match="plan_prerequisites_changed"
    ):
        await service.claim(operation.operation_id, expected_revision=1)
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        assert plan.status == OrganizationPlanStatus.INVALIDATED.value
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_or_scope_change_invalidates_plan_and_blocks_claim(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, LIBRARY_ID)
        assert library is not None
        library.scope_verified = False
        await session.commit()

    with pytest.raises(OrganizationOperationPrerequisiteError):
        await service.claim(operation.operation_id, expected_revision=1)
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.PLANNED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_library_revision_change_invalidates_plan_and_blocks_claim(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, LIBRARY_ID)
        assert library is not None
        library.revision += 1
        await session.commit()

    with pytest.raises(OrganizationOperationPrerequisiteError):
        await service.claim(operation.operation_id, expected_revision=1)
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.PLANNED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_renew_lease_fences_changed_plan_revision(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="renew-plan-fence")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        plan.revision += 1
        await session.commit()

    with pytest.raises(
        OrganizationOperationLeaseUnavailable, match="plan_revision_changed"
    ):
        await service.renew_lease(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.error_code == "plan_prerequisites_changed"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_completion_fences_changed_plan_revision(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="complete-plan-fence")
    lease = await service.claim(operation.operation_id, expected_revision=1)

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        plan.revision += 1
        await session.commit()

    with pytest.raises(
        OrganizationOperationLeaseUnavailable, match="plan_revision_changed"
    ):
        await service.complete_organized_with_dirty_events(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            source_directory_id="7000",
            target_directory_id="8000",
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.error_code == "plan_prerequisites_changed"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_recovers_after_reopening_database(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    lease = await service.claim(
        operation.operation_id,
        expected_revision=1,
        lease_duration=timedelta(seconds=1),
        now=now,
    )
    database_path = tmp_path / "operations.db"
    await database.engine.dispose()

    reopened = create_database(f"sqlite+aiosqlite:///{database_path}")
    await initialize_database(reopened.engine)
    reopened_service = OrganizationOperationService(reopened.session_factory)
    with pytest.raises(OrganizationOperationStateError, match="uncertain_requires_verification"):
        await reopened_service.claim(
            operation.operation_id,
            expected_revision=lease.revision,
            now=now + timedelta(seconds=2),
        )
    current = await reopened_service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.revision == lease.revision + 1
    assert current.error_code == "outcome_unknown"
    await reopened.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_different_keys_create_one_operation(tmp_path):
    for round_number in range(8):
        round_path = tmp_path / str(round_number)
        round_path.mkdir()
        database = await _database(round_path)
        plan = await _plan(database)
        first = OrganizationOperationService(database.session_factory)
        second = OrganizationOperationService(database.session_factory)

        results = await asyncio.gather(
            first.create(plan.plan_id, idempotency_key="key-a"),
            second.create(plan.plan_id, idempotency_key="key-b"),
            return_exceptions=True,
        )
        conflicts = [
            result
            for result in results
            if isinstance(result, OrganizationOperationConflict)
        ]
        assert len(conflicts) == 1
        assert str(conflicts[0]) == "operation_plan_conflict"
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        async with database.session_factory() as session:
            assert len(list(await session.scalars(select(OrganizationOperation)))) == 1
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_allows_new_operation_after_failed_operation(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="first-failed-attempt")
    lease = await service.claim(operation.operation_id, expected_revision=1)
    failed = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.FAILED,
        error_code="local_failure",
    )
    assert failed.status is OrganizationOperationStatus.FAILED

    retry = await service.create(
        operation.plan_id, idempotency_key="retry-after-failure"
    )
    assert retry.operation_id != operation.operation_id
    assert retry.plan_id == operation.plan_id
    assert retry.status is OrganizationOperationStatus.PLANNED
    assert (await service.get_for_plan(operation.plan_id)).operation_id == retry.operation_id
    async with database.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(OrganizationOperation).where(
                        OrganizationOperation.plan_id == operation.plan_id
                    )
                )
            ).all()
        )
        assert len(rows) == 2
        assert {row.status for row in rows} == {
            OrganizationOperationStatus.FAILED,
            OrganizationOperationStatus.PLANNED,
        }
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_allows_new_operation_after_cancelled_operation(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="first-cancelled-attempt")
    cancelled = await service.cancel(operation.operation_id, expected_revision=1)
    assert cancelled.status is OrganizationOperationStatus.CANCELLED

    retry = await service.create(
        operation.plan_id, idempotency_key="retry-after-cancel"
    )
    assert retry.operation_id != operation.operation_id
    assert retry.plan_id == operation.plan_id
    assert retry.status is OrganizationOperationStatus.PLANNED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_conflicts_while_planned_operation_queued(tmp_path):
    database = await _database(tmp_path)
    operation = await _operation(database, key="queued-attempt")
    with pytest.raises(OrganizationOperationConflict, match="operation_plan_conflict"):
        await OrganizationOperationService(database.session_factory).create(
            operation.plan_id, idempotency_key="different-key"
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_conflicts_while_operation_organizing(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="running-attempt")
    await service.claim(operation.operation_id, expected_revision=1)
    with pytest.raises(OrganizationOperationConflict, match="operation_plan_conflict"):
        await service.create(
            operation.plan_id, idempotency_key="different-key"
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_conflicts_while_operation_uncertain(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="uncertain-attempt")
    lease = await service.claim(operation.operation_id, expected_revision=1)
    uncertain = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.UNCERTAIN,
        error_code="outcome_unknown",
    )
    assert uncertain.status is OrganizationOperationStatus.UNCERTAIN
    with pytest.raises(OrganizationOperationConflict, match="operation_plan_conflict"):
        await service.create(
            operation.plan_id, idempotency_key="different-key"
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_idempotency_replay_returns_existing_summary(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="idempotent-create")
    repeated = await service.create(
        operation.plan_id, idempotency_key="idempotent-create"
    )
    assert repeated.operation_id == operation.operation_id
    assert repeated.plan_id == operation.plan_id
    assert repeated.status is OrganizationOperationStatus.PLANNED
    assert repeated.revision == operation.revision
    async with database.session_factory() as session:
        assert len(list(await session.scalars(select(OrganizationOperation)))) == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_old_lease_token_cannot_finish_after_reclaim(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    lease = await service.claim(
        operation.operation_id,
        expected_revision=1,
        lease_duration=timedelta(seconds=1),
        now=now,
    )
    with pytest.raises(OrganizationOperationStateError, match="uncertain_requires_verification"):
        await service.claim(
            operation.operation_id,
            expected_revision=lease.revision,
            now=now + timedelta(seconds=2),
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN

    with pytest.raises(OrganizationOperationLeaseUnavailable):
        await service.finish(
            operation.operation_id,
            expected_revision=current.revision,
            lease_token=lease.lease_token,
            status=OrganizationOperationStatus.ORGANIZED,
            now=now + timedelta(seconds=2),
            source_directory_id="source-dir",
            target_directory_id="target-dir",
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_uncertain_rejects_retry_and_error_codes_are_allowlisted(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    lease = await service.claim(operation.operation_id, expected_revision=1)

    with pytest.raises(ValueError, match="invalid_error_code"):
        await service.finish(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            status=OrganizationOperationStatus.FAILED,
            error_code="cookie=secret",
        )
    uncertain = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.UNCERTAIN,
        error_code="outcome_unknown",
    )
    with pytest.raises(OrganizationOperationStateError):
        await service.retry(
            operation.operation_id, expected_revision=uncertain.revision
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_does_not_commit_after_plan_revision_change(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="reconcile-plan-fence")
    lease = await service.claim(operation.operation_id, expected_revision=1)
    uncertain = await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.UNCERTAIN,
        error_code="outcome_unknown",
    )

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        plan.revision += 1
        await session.commit()

    with pytest.raises(OrganizationOperationConflict, match="plan_revision_changed"):
        await service.reconcile_not_applied(
            operation.operation_id, expected_revision=uncertain.revision
        )
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_operation_schema_defaults_and_unique_keys(tmp_path):
    database = await _database(tmp_path)
    async with database.engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync).get_columns("organization_operations")
            }
        )
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("organization_operations")
        )
    assert {
        "id",
        "plan_id",
        "idempotency_key",
        "status",
        "revision",
        "attempts",
    } <= columns
    assert any(
        index["unique"] and index["column_names"] == ["plan_id"] for index in indexes
    )
    active_plan_index = next(
        index
        for index in indexes
        if index["name"] == "uq_organization_operations_active_plan"
    )
    assert active_plan_index["unique"]
    assert active_plan_index["column_names"] == ["plan_id"]
    operation = await _operation(database)
    assert operation.status is OrganizationOperationStatus.PLANNED
    assert operation.revision == 1
    assert operation.attempts == 0
    repeated = await OrganizationOperationService(database.session_factory).create(
        operation.plan_id, idempotency_key="operation-1"
    )
    assert repeated.operation_id == operation.operation_id
    with pytest.raises(OrganizationOperationConflict, match="operation_plan_conflict"):
        await OrganizationOperationService(database.session_factory).create(
            operation.plan_id, idempotency_key="different-key"
        )
    await database.engine.dispose()


def test_operation_repr_redacts_identifiers():
    operation = OrganizationOperation(
        id="op-secret-id",
        plan_id="plan-secret-id",
        plan_revision=1,
        idempotency_key="idempotency-secret",
    )
    assert "secret" not in repr(operation)


@pytest.mark.asyncio
async def test_concurrent_expired_claim_next_does_not_double_increment_revision(
    tmp_path: Path,
):
    """并发 claim_next 同时转换同一过期 ORGANIZING 操作时,revision 必须
    只递增一次(原子 CAS),不得因 SELECT 后逐行 ORM 修改而重复 +2。"""
    database = await _database(tmp_path)
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database, key="expired-concurrent")
    first = await service.claim(
        operation.operation_id,
        expected_revision=1,
        lease_duration=timedelta(seconds=1),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    # 原子转换:两个并发 claim_next 同时转换同一过期操作,最终只执行一次
    # _sync_workflow_stage(单条 UPDATE CAS),而非重复发 workflow 事件。
    import asyncio as _asyncio

    from watch_assistant.services.organization_operations import (
        _sync_workflow_stage as _real_sync,
    )

    sync_calls = 0
    import watch_assistant.services.organization_operations as org_ops_module
    original_sync = org_ops_module._sync_workflow_stage

    async def counting_sync(*args, **kwargs):
        nonlocal sync_calls
        sync_calls += 1
        return await _real_sync(*args, **kwargs)

    org_ops_module._sync_workflow_stage = counting_sync

    async def convert_once():
        try:
            return await service.claim_next(
                now=datetime(2026, 7, 28, 0, 0, 2, tzinfo=UTC)
            )
        finally:
            pass

    results = await _asyncio.gather(convert_once(), convert_once())
    org_ops_module._sync_workflow_stage = original_sync

    assert all(r is None for r in results)
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.revision == first.revision + 1, f"revision={current.revision}"
    assert current.error_code == "outcome_unknown"
    # 原子转换:同一过期操作只应被转换一次(单条 UPDATE CAS),
    # 而非两个协程各执行一次 _sync_workflow_stage(重复发 workflow 事件)。
    assert sync_calls == 1, f"_sync_workflow_stage 被调 {sync_calls} 次"
    await database.engine.dispose()
