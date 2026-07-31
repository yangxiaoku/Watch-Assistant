import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from test_organization_operations import _database, _item, _operation

from watch_assistant.library_models import (
    LibraryScanEntry,
    OrganizationHistoryEntry,
    OrganizationPlan,
)
from watch_assistant.models import (
    DirectoryDirtyEvent,
    OrganizationOperation,
    OrganizationOperationStatus,
)
from watch_assistant.services.organization_executor import (
    OrganizationExecutionStatus,
    OrganizationExecutor,
    OrganizationTransportOperation,
    OrganizationTransportResult,
    OrganizationTransportStatus,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanCompanion,
    OrganizationPlanService,
    PlanSource,
)
from watch_assistant.services.organization_policy import VersionDecision


class FakeOrganizationTransport:
    def __init__(
        self,
        *,
        states=None,
        move_outcomes=None,
        rename_outcomes=None,
        post_mismatch=False,
    ):
        self.states = dict(states or {"100": ("7000", "movie.mkv")})
        self.move_outcomes = dict(move_outcomes or {})
        self.rename_outcomes = dict(rename_outcomes or {})
        self.post_mismatch = post_mismatch
        self.calls = []

    async def read_object(self, object_id):
        self.calls.append(("read_object", object_id))
        state = self.states.get(object_id)
        if state is None:
            return None
        parent_id, name = state
        from watch_assistant.services.organization_execution_contract import (
            RemoteObjectState,
        )

        return RemoteObjectState(object_id, parent_id, name)

    async def read_target(self, parent_id, name):
        self.calls.append(("read_target", parent_id, name))
        for object_id, state in self.states.items():
            if state == (parent_id, name):
                from watch_assistant.services.organization_execution_contract import (
                    RemoteObjectState,
                )

                return RemoteObjectState(object_id, parent_id, name)
        return None

    async def move(self, object_id, target_parent_id):
        self.calls.append(("move", object_id, target_parent_id))
        outcome = self.move_outcomes.get(object_id)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        _, name = self.states[object_id]
        self.states[object_id] = (target_parent_id, name)
        return OrganizationTransportResult(
            OrganizationTransportOperation.MOVE, OrganizationTransportStatus.SUCCESS
        )

    async def rename(self, object_id, target_name):
        self.calls.append(("rename", object_id, target_name))
        outcome = self.rename_outcomes.get(object_id)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        parent_id, _name = self.states[object_id]
        self.states[object_id] = (
            parent_id,
            "post-mismatch.mkv" if self.post_mismatch else target_name,
        )
        return OrganizationTransportResult(
            OrganizationTransportOperation.RENAME, OrganizationTransportStatus.SUCCESS
        )

    async def recycle(self, object_id, parent_id, name):
        self.calls.append(("recycle", object_id, parent_id, name))
        if self.states.get(object_id) != (parent_id, name):
            return OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        del self.states[object_id]
        return OrganizationTransportResult(
            OrganizationTransportOperation.RECYCLE, OrganizationTransportStatus.SUCCESS
        )


async def _claimed_executor(database, transport, **kwargs):
    operation_service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    executor = OrganizationExecutor(
        operation_service,
        database.session_factory,
        transport,
        **kwargs,
    )
    return executor, operation, lease


@pytest.mark.asyncio
async def test_executor_success_is_ordered_and_completes_outbox(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    assert result.completed_steps == 1
    assert transport.calls == [
        ("read_object", "100"),
        ("read_target", "8000", "movie.mkv"),
        ("move", "100", "8000"),
        ("rename", "100", "movie.mkv"),
        ("read_object", "100"),
    ]
    async with database.session_factory() as session:
        events = list(await session.scalars(select(DirectoryDirtyEvent)))
        history = list(await session.scalars(select(OrganizationHistoryEntry)))
    assert {event.directory_id for event in events} == {"7000", "8000"}
    assert len(history) == 1
    assert history[0].title == "Movie"
    assert history[0].target_path == "movie/movie.mkv"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_exact_target_replay_is_success_without_write(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(states={"100": ("8000", "movie.mkv")})
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    assert result.completed_steps == 1
    assert all(call[0] not in {"move", "rename"} for call in transport.calls)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_candidate_replacement_recycles_existing_target_before_write(tmp_path: Path):
    database = await _database(tmp_path)
    plan_service = OrganizationPlanService(database.session_factory)
    plan = await plan_service.create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(
            replace(
                _item(),
                policy_decision=VersionDecision("candidate", "remux_priority", "remux"),
                replacement_object_id="200",
                replacement_parent_id="8000",
                replacement_name="movie.mkv",
            ),
        ),
    )
    operation_service = OrganizationOperationService(database.session_factory)
    operation = await operation_service.create(plan.plan_id, idempotency_key="replace")
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "200": ("8000", "movie.mkv")}
    )
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    assert [call[0] for call in transport.calls] == [
        "read_object",
        "read_target",
        "recycle",
        "read_target",
        "move",
        "rename",
        "read_object",
    ]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_invalid_durable_steps_fail_without_guessing_or_transport(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, operation.plan_id)
        assert plan is not None
        actions = json.loads(plan.actions_json)
        actions[0].pop("execution")
        plan.actions_json = json.dumps(actions)
        await session.commit()

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.FAILED,
        "plan_prerequisites_changed",
    )
    assert transport.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_precondition_conflict_finishes_failed_without_writes(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(states={"100": ("7000", "changed.mkv")})
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.FAILED,
        "plan_prerequisites_changed",
    )
    assert all(call[0] not in {"move", "rename"} for call in transport.calls)
    assert (
        await OrganizationOperationService(database.session_factory).get(
            operation.operation_id
        )
    ).status is OrganizationOperationStatus.FAILED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_target_conflict_is_failed_even_if_source_is_at_target(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(
        states={"other": ("8000", "movie.mkv"), "100": ("8000", "movie.mkv")}
    )
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.FAILED
    assert all(call[0] not in {"move", "rename"} for call in transport.calls)
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code"),
    (
        (
            OrganizationTransportResult(
                OrganizationTransportOperation.MOVE,
                OrganizationTransportStatus.FAILED,
            ),
            OrganizationExecutionStatus.FAILED,
            "remote_write_failed",
        ),
        (
            TimeoutError("private detail"),
            OrganizationExecutionStatus.UNCERTAIN,
            "timeout",
        ),
    ),
)
async def test_write_failure_stops_without_retry(
    tmp_path: Path, outcome, expected_status, expected_code
):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(move_outcomes={"100": outcome})
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (expected_status, expected_code)
    assert [call[0] for call in transport.calls].count("move") == 1
    assert all(call[0] != "rename" for call in transport.calls)
    assert len(transport.calls) == 3
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_confirmed_move_then_failed_rename_is_uncertain(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(
        rename_outcomes={
            "100": OrganizationTransportResult(
                OrganizationTransportOperation.RENAME,
                OrganizationTransportStatus.FAILED,
            )
        }
    )
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "remote_write_failed",
    )
    assert [call[0] for call in transport.calls] == [
        "read_object",
        "read_target",
        "move",
        "rename",
    ]
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_postcondition_mismatch_is_uncertain_and_stops(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(post_mismatch=True)
    executor, operation, lease = await _claimed_executor(database, transport)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "postcondition_mismatch",
    )
    assert all(call[0] != "move" or call[1] == "100" for call in transport.calls)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_point_finishes_cancelled_without_transport_call(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        cancel_event=cancel_event,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.CANCELLED,
        "cancelled",
    )
    assert transport.calls == []
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.CANCELLED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_stops_before_transport(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)
    async with database.session_factory() as session:
        row = await session.get(
            OrganizationOperation,
            operation.operation_id,
        )
        assert row is not None
        row.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
        await session.commit()

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.LEASE_LOST
    assert transport.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_call_rechecks_cancel_after_rate_limit_sleep(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(
        database, transport, min_call_interval=1
    )
    cancel_event = asyncio.Event()

    async def cancel_after_sleep(_delay: float) -> None:
        cancel_event.set()

    executor = OrganizationExecutor(
        OrganizationOperationService(database.session_factory),
        database.session_factory,
        transport,
        min_call_interval=1,
        sleep=cancel_after_sleep,
    )
    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        cancel_event=cancel_event,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.CANCELLED,
        "cancelled",
    )
    assert [call[0] for call in transport.calls] == ["read_object"]
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.CANCELLED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_persistent_cancel_request_stops_before_transport(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)
    service = OrganizationOperationService(database.session_factory)
    await service.cancel(operation.operation_id, expected_revision=lease.revision)

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.CANCELLED,
        "cancelled",
    )
    assert transport.calls == []
    current = await service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.CANCELLED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_call_rechecks_lease_after_rate_limit_sleep(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(
        database, transport, min_call_interval=1
    )

    async def lose_lease_after_sleep(_delay: float) -> None:
        async with database.session_factory() as session:
            row = await session.get(OrganizationOperation, operation.operation_id)
            assert row is not None
            row.lease_token = "z" * 32
            await session.commit()

    executor = OrganizationExecutor(
        OrganizationOperationService(database.session_factory),
        database.session_factory,
        transport,
        min_call_interval=1,
        sleep=lose_lease_after_sleep,
    )
    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.LEASE_LOST,
        "lease_lost",
    )
    assert [call[0] for call in transport.calls] == ["read_object"]
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.ORGANIZING
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_lease_loss_after_write_persists_uncertain(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(database, transport)
    move = transport.move

    async def move_then_lose_lease(object_id, target_parent_id):
        result = await move(object_id, target_parent_id)
        async with database.session_factory() as session:
            row = await session.get(OrganizationOperation, operation.operation_id)
            assert row is not None
            row.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
            await session.commit()
        return result

    transport.move = move_then_lose_lease
    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "lease_lost",
    )
    assert all(call[0] != "rename" for call in transport.calls)
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.error_code == "lease_lost"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_rate_limit_stops_before_first_write(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    executor, operation, lease = await _claimed_executor(
        database, transport, max_transport_calls=2
    )

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "rate_limited",
    )
    assert all(call[0] not in {"move", "rename"} for call in transport.calls)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_companion_partial_failure_does_not_continue_group(tmp_path: Path):
    database = await _database(tmp_path)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-1",
                object_type="file",
                object_id="102",
                parent_id="7000",
                name="movie.srt",
                path="/private/movie.srt",
                is_directory=False,
            )
        )
        await session.commit()
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id="7000",
            path="/private/movie.srt",
            remote_version="remote-v1",
        ),
        target_parent_id="8000",
        target_name="movie.srt",
    )
    plan = await OrganizationPlanService(database.session_factory).create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(replace(_item(), companions=(companion,)),),
    )
    operation = await OrganizationOperationService(database.session_factory).create(
        plan.plan_id, idempotency_key="companion-operation"
    )
    operation_service = OrganizationOperationService(database.session_factory)
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "102": ("7000", "movie.srt")},
        move_outcomes={
            "102": OrganizationTransportResult(
                OrganizationTransportOperation.MOVE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        },
    )
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.UNCERTAIN
    assert ("rename", "102", "movie.srt") not in transport.calls
    assert result.completed_steps == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_companion_partial_replay_is_uncertain_without_writes(tmp_path: Path):
    database = await _database(tmp_path)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-1",
                object_type="file",
                object_id="102",
                parent_id="7000",
                name="movie.srt",
                path="/private/movie.srt",
                is_directory=False,
            )
        )
        await session.commit()
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id="7000",
            path="/private/movie.srt",
            remote_version="remote-v1",
        ),
        target_parent_id="8000",
        target_name="movie.srt",
    )
    plan = await OrganizationPlanService(database.session_factory).create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(replace(_item(), companions=(companion,)),),
    )
    operation = await OrganizationOperationService(database.session_factory).create(
        plan.plan_id, idempotency_key="partial-replay-operation"
    )
    operation_service = OrganizationOperationService(database.session_factory)
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    transport = FakeOrganizationTransport(
        states={"100": ("8000", "movie.mkv"), "102": ("7000", "movie.srt")}
    )
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "outcome_unknown",
    )
    assert all(call[0] not in {"move", "rename"} for call in transport.calls)
    current = await operation_service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_task_cancellation_marks_uncertain_and_propagates(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(
        move_outcomes={"100": asyncio.CancelledError()}
    )
    executor, operation, lease = await _claimed_executor(database, transport)

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
        )
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.error_code == "cancelled"
    await database.engine.dispose()
