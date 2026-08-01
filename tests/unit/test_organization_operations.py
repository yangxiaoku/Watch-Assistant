import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    OrganizationOperation,
    OrganizationOperationStatus,
)
from watch_assistant.schemas import (
    MediaType,
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
)
from watch_assistant.services.workflows import WorkflowService

LIBRARY_ID = "library-1"
ROOT_ID = "7000"
SCAN_ID = "scan-1"


def _item(
    *, confidence: MatchConfidence = MatchConfidence.HIGH
) -> OrganizationPlanItem:
    return OrganizationPlanItem(
        source=PlanSource(
            object_type="file",
            object_id="100",
            parent_id=ROOT_ID,
            path="/private/movie.mkv",
            remote_version="remote-v1",
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
                state="completed",
                complete=True,
                snapshot_revision=1,
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
        await session.commit()
    return database


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
        WorkflowCreateRequest(media_type=MediaType.MOVIE, tmdb_id=1)
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
    for stage_name in (
        WorkflowStageName.DISCOVERY,
        WorkflowStageName.INSPECTION,
        WorkflowStageName.APPROVAL,
        WorkflowStageName.PUSH,
        WorkflowStageName.AVAILABILITY,
    ):
        await workflow_service.patch_stage(
            workflow.id,
            stage_name,
            WorkflowStagePatch(status=WorkflowStageStatus.RUNNING),
        )
        if stage_name is WorkflowStageName.APPROVAL:
            await workflow_service.patch_stage(
                workflow.id,
                stage_name,
                WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
            )
        else:
            await workflow_service.patch_stage(
                workflow.id,
                stage_name,
                WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
            )

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
    recovered = await OrganizationOperationService(reopened.session_factory).claim(
        operation.operation_id,
        expected_revision=lease.revision,
        now=now + timedelta(seconds=2),
    )
    assert recovered.revision == 3
    assert recovered.lease_token != lease.lease_token
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
    reclaimed = await service.claim(
        operation.operation_id,
        expected_revision=lease.revision,
        now=now + timedelta(seconds=2),
    )
    assert reclaimed.lease_token != lease.lease_token

    with pytest.raises(OrganizationOperationLeaseUnavailable):
        await service.finish(
            operation.operation_id,
            expected_revision=reclaimed.revision,
            lease_token=lease.lease_token,
            status=OrganizationOperationStatus.ORGANIZED,
            now=now + timedelta(seconds=2),
            source_directory_id="source-dir",
            target_directory_id="target-dir",
        )
    finished = await service.finish(
        operation.operation_id,
        expected_revision=reclaimed.revision,
        lease_token=reclaimed.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        now=now + timedelta(seconds=2),
        source_directory_id="source-dir",
        target_directory_id="target-dir",
    )
    assert finished.status is OrganizationOperationStatus.ORGANIZED
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
