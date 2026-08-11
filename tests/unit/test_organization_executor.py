import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from test_organization_operations import (
    _database,
    _item,
    _operation,
    _refresh_completed_tree_evidence,
)

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
    OrganizationExecutorError,
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
    _entry_remote_version,
)
from watch_assistant.services.organization_policy import VersionDecision


def _source_version(object_id: str, path: str, name: str) -> str:
    return _entry_remote_version(
        LibraryScanEntry(
            scan_run_id="scan-1",
            object_type="file",
            object_id=object_id,
            parent_id="7000",
            name=name,
            path=path,
            is_directory=False,
        )
    )


class FakeOrganizationTransport:
    def __init__(
        self,
        *,
        states=None,
        move_outcomes=None,
        rename_outcomes=None,
        recycle_outcomes=None,
        post_mismatch=False,
    ):
        self.states = dict(states or {"100": ("7000", "movie.mkv")})
        self.move_outcomes = dict(move_outcomes or {})
        self.rename_outcomes = dict(rename_outcomes or {})
        self.recycle_outcomes = dict(recycle_outcomes or {})
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

    async def read_object_in_scope(self, object_id, parent_ids):
        self.calls.append(("read_object_in_scope", object_id, tuple(parent_ids)))
        state = self.states.get(object_id)
        if state is None or state[0] not in set(parent_ids):
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
        outcome = self.recycle_outcomes.get(object_id)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        if self.states.get(object_id) != (parent_id, name):
            return OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        del self.states[object_id]
        return OrganizationTransportResult(
            OrganizationTransportOperation.RECYCLE, OrganizationTransportStatus.SUCCESS
        )


async def _noop_sleep(_delay: float) -> None:
    return None


async def _claimed_executor(database, transport, **kwargs):
    operation_service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    # 默认注入 no-op sleep:间隔逻辑由专门的注入测试覆盖,避免测试变慢。
    kwargs.setdefault("sleep", _noop_sleep)
    executor = OrganizationExecutor(
        operation_service,
        database.session_factory,
        transport,
        **kwargs,
    )
    return executor, operation, lease


async def _replacement_operation(database, *, key: str):
    plan_service = OrganizationPlanService(database.session_factory)
    plan = await plan_service.create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(
            replace(
                _item(),
                policy_decision=VersionDecision(
                    "candidate", "remux_priority", "remux"
                ),
                replacement_object_id="200",
                replacement_parent_id="8000",
                replacement_name="movie.mkv",
            ),
        ),
    )
    operation_service = OrganizationOperationService(database.session_factory)
    operation = await operation_service.create(plan.plan_id, idempotency_key=key)
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    return operation_service, operation, lease


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
        operation_service, database.session_factory, transport, sleep=_noop_sleep
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
async def test_missing_replacement_skips_recycle_and_continues(tmp_path: Path):
    # 垃圾文件已不在目标位置(回收已满足,例如回收后崩溃再重放):
    # 跳过 recycle 直接移动,而不是把整个计划 invalidate。
    database = await _database(tmp_path)
    operation_service, operation, lease = await _replacement_operation(
        database, key="replacement-already-removed"
    )
    transport = FakeOrganizationTransport(states={"100": ("7000", "movie.mkv")})
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    assert [call[0] for call in transport.calls] == [
        "read_object",
        "read_target",
        "move",
        "rename",
        "read_object",
    ]
    current = await operation_service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.ORGANIZED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_replacement_position_holds_other_object_fails_closed(tmp_path: Path):
    # 回收前提不满足:垃圾位置存在的是其他对象(且不在受管范围),
    # 必须 fail-closed,不得当作"回收已满足"跳过。
    database = await _database(tmp_path)
    operation_service, operation, lease = await _replacement_operation(
        database, key="replacement-position-conflict"
    )
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "300": ("8000", "movie.mkv")}
    )
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.FAILED,
        "plan_prerequisites_changed",
    )
    assert all(call[0] not in {"recycle", "move", "rename"} for call in transport.calls)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_postcondition_mismatch_finishes_uncertain(tmp_path: Path):
    # 回收成功后目标位置仍被占用:必须以 cleanup_postcondition_mismatch
    # 持久化为 UNCERTAIN,不能因错误码不在白名单而抛 ValueError。
    database = await _database(tmp_path)
    operation_service, operation, lease = await _replacement_operation(
        database, key="cleanup-postcondition-mismatch"
    )
    transport = FakeOrganizationTransport(
        states={
            "100": ("7000", "movie.mkv"),
            "200": ("8000", "movie.mkv"),
            "201": ("8000", "movie.mkv"),
        }
    )
    result = await OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    ).execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert (result.status, result.error_code) == (
        OrganizationExecutionStatus.UNCERTAIN,
        "cleanup_postcondition_mismatch",
    )
    assert ("recycle", "200", "8000", "movie.mkv") in transport.calls
    current = await operation_service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.UNCERTAIN
    assert current.error_code == "cleanup_postcondition_mismatch"
    await database.engine.dispose()


def test_safe_error_code_maps_unknown_codes_to_outcome_unknown():
    from watch_assistant.services.organization_executor import _safe_error_code

    assert _safe_error_code(None) is None
    assert _safe_error_code("cleanup_postcondition_mismatch") == (
        "cleanup_postcondition_mismatch"
    )
    assert _safe_error_code("target_root_changed") == "target_root_changed"
    assert _safe_error_code("not_a_real_code") == "outcome_unknown"


@pytest.mark.asyncio
async def test_uncertain_recycle_with_removed_replacement_is_retryable(
    tmp_path: Path,
):
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
    operation = await operation_service.create(plan.plan_id, idempotency_key="replace-uncertain")
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "200": ("8000", "movie.mkv")},
        recycle_outcomes={
            "200": OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        },
    )
    executor = OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    )
    initial = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    assert initial.status is OrganizationExecutionStatus.UNCERTAIN

    # 回收可能已生效(垃圾已离开受管范围)而成员仍在源:这是回收成功但
    # 移动未发生的中间态,必须判定为可重试的 NOT_APPLIED,而不是让计划
    # 永久停留在 UNCERTAIN 卡死。
    transport.states.pop("200")
    summary = await operation_service.get(operation.operation_id)
    call_count = len(transport.calls)
    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert (reconciled.status, reconciled.error_code) == (
        OrganizationExecutionStatus.FAILED,
        "replacement_already_removed",
    )
    assert [call[0] for call in transport.calls[call_count:]] == [
        "read_object",
        "read_object_in_scope",
    ]
    assert all(
        call[0] not in {"move", "rename", "recycle"}
        for call in transport.calls[call_count:]
    )
    current = await operation_service.get(operation.operation_id)
    assert current.status is OrganizationOperationStatus.FAILED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_target_reconciliation_confirms_missing_replacement_in_frozen_scope(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    operation_service, operation, lease = await _replacement_operation(
        database, key="replacement-target-reconciliation"
    )
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "200": ("8000", "movie.mkv")},
        recycle_outcomes={
            "200": OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        },
    )
    executor = OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    )

    initial = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    assert initial.status is OrganizationExecutionStatus.UNCERTAIN

    transport.states["100"] = ("8000", "movie.mkv")
    transport.states.pop("200")
    summary = await operation_service.get(operation.operation_id)
    call_count = len(transport.calls)

    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert (reconciled.status, reconciled.error_code) == (
        OrganizationExecutionStatus.ORGANIZED,
        None,
    )
    assert [call[0] for call in transport.calls[call_count:]] == [
        "read_object",
        "read_object_in_scope",
    ]
    assert all(
        call[0] not in {"move", "rename", "recycle"}
        for call in transport.calls[call_count:]
    )
    assert (
        await operation_service.get(operation.operation_id)
    ).status is OrganizationOperationStatus.ORGANIZED
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_state", "expected_status"),
    (
        (("7000", "movie.mkv"), OrganizationExecutionStatus.UNCERTAIN),
        (("8000", "movie.mkv"), OrganizationExecutionStatus.UNCERTAIN),
    ),
    ids=("replacement-at-source", "replacement-at-target"),
)
async def test_reconciliation_keeps_non_removed_replacement_observations_uncertain(
    tmp_path: Path,
    replacement_state: tuple[str, str],
    expected_status: OrganizationExecutionStatus,
):
    database = await _database(tmp_path)
    operation_service, operation, lease = await _replacement_operation(
        database, key=f"replacement-state-{replacement_state[0]}"
    )
    transport = FakeOrganizationTransport(
        states={"100": ("7000", "movie.mkv"), "200": ("8000", "movie.mkv")},
        recycle_outcomes={
            "200": OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        },
    )
    executor = OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    )

    await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    transport.states["100"] = ("8000", "movie.mkv")
    transport.states["200"] = replacement_state
    summary = await operation_service.get(operation.operation_id)
    call_count = len(transport.calls)

    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert reconciled.status is expected_status
    assert all(
        call[0] not in {"move", "rename", "recycle"}
        for call in transport.calls[call_count:]
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_mixed_steps_complete_when_replacement_leaves_frozen_scope(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-1",
                object_type="file",
                object_id="101",
                parent_id="7000",
                name="episode.mkv",
                path="/private/episode.mkv",
                is_directory=False,
            )
        )
        await session.commit()
    await _refresh_completed_tree_evidence(database)
    first = replace(
        _item(),
        policy_decision=VersionDecision("candidate", "remux_priority", "remux"),
        replacement_object_id="200",
        replacement_parent_id="8000",
        replacement_name="movie.mkv",
    )
    second = replace(
        _item(),
        source=replace(
            _item().source,
            object_id="101",
            path="/private/episode.mkv",
            remote_version=_source_version(
                "101", "/private/episode.mkv", "episode.mkv"
            ),
        ),
        naming_plan=replace(
            _item().naming_plan,
            target_path="movie/episode.mkv",
            display_name="Episode",
        ),
        target_name="episode.mkv",
    )
    plan = await OrganizationPlanService(database.session_factory).create_plan(
        library_id="library-1",
        scan_run_id="scan-1",
        items=(first, second),
    )
    operation_service = OrganizationOperationService(database.session_factory)
    operation = await operation_service.create(
        plan.plan_id, idempotency_key="mixed-reconciliation"
    )
    lease = await operation_service.claim(operation.operation_id, expected_revision=1)
    transport = FakeOrganizationTransport(
        states={
            "100": ("7000", "movie.mkv"),
            "101": ("7000", "episode.mkv"),
            "200": ("8000", "movie.mkv"),
        },
        recycle_outcomes={
            "200": OrganizationTransportResult(
                OrganizationTransportOperation.RECYCLE,
                OrganizationTransportStatus.UNCERTAIN,
            )
        },
    )
    executor = OrganizationExecutor(
        operation_service, database.session_factory, transport, sleep=_noop_sleep
    )

    initial = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    assert initial.status is OrganizationExecutionStatus.UNCERTAIN

    transport.states["100"] = ("8000", "movie.mkv")
    transport.states["101"] = ("8000", "episode.mkv")
    transport.states.pop("200")
    summary = await operation_service.get(operation.operation_id)

    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert reconciled.status is OrganizationExecutionStatus.ORGANIZED
    assert (
        await operation_service.get(operation.operation_id)
    ).status is OrganizationOperationStatus.ORGANIZED
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
async def test_uncertain_reconciliation_reads_only_and_commits_exact_target(tmp_path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(move_outcomes={"100": TimeoutError()})
    executor, operation, lease = await _claimed_executor(database, transport)

    initial = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    assert initial.status is OrganizationExecutionStatus.UNCERTAIN
    summary = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    transport.states["100"] = ("8000", "movie.mkv")
    call_count = len(transport.calls)

    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert reconciled.status is OrganizationExecutionStatus.ORGANIZED
    assert [call[0] for call in transport.calls[call_count:]] == ["read_object"]
    assert all(call[0] not in {"move", "rename", "recycle"} for call in transport.calls[call_count:])
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.ORGANIZED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_uncertain_reconciliation_marks_exact_source_retryable(tmp_path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport(move_outcomes={"100": TimeoutError()})
    executor, operation, lease = await _claimed_executor(database, transport)

    await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )
    summary = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )

    reconciled = await executor.reconcile_uncertain(
        operation.operation_id,
        expected_revision=summary.revision,
    )

    assert (reconciled.status, reconciled.error_code) == (
        OrganizationExecutionStatus.FAILED,
        "remote_write_failed",
    )
    current = await OrganizationOperationService(database.session_factory).get(
        operation.operation_id
    )
    assert current.status is OrganizationOperationStatus.FAILED
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
async def test_write_calls_use_write_interval_and_reads_use_min_interval(tmp_path: Path):
    database = await _database(tmp_path)
    transport = FakeOrganizationTransport()
    sleep_delays = []

    async def recording_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    executor, operation, lease = await _claimed_executor(
        database,
        transport,
        min_call_interval=0.5,
        write_interval_seconds=4.0,
        sleep=recording_sleep,
    )

    result = await executor.execute(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
    )

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    # 调用序列:read_object → read_target → move → rename → read_object,
    # 共 4 次间隔等待;读写之间的等待值分别为 min_call_interval 与 write_interval。
    assert len(sleep_delays) == 4
    assert sleep_delays[0] == pytest.approx(0.5, abs=0.02)
    assert sleep_delays[1] == pytest.approx(4.0, abs=0.02)
    assert sleep_delays[2] == pytest.approx(4.0, abs=0.02)
    assert sleep_delays[3] == pytest.approx(0.5, abs=0.02)
    await database.engine.dispose()


def test_write_interval_defaults_and_validation():
    executor = OrganizationExecutor(object(), object(), object(), min_call_interval=2.0)
    assert executor._write_interval == pytest.approx(6.0)
    executor = OrganizationExecutor(object(), object(), object())
    assert executor._write_interval == pytest.approx(3.0)
    executor = OrganizationExecutor(
        object(), object(), object(), write_interval_seconds=5.0
    )
    assert executor._write_interval == pytest.approx(5.0)
    with pytest.raises(OrganizationExecutorError, match="invalid_execution_budget"):
        OrganizationExecutor(object(), object(), object(), write_interval_seconds=-1)
    with pytest.raises(OrganizationExecutorError, match="invalid_execution_budget"):
        OrganizationExecutor(object(), object(), object(), write_interval_seconds=True)


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
    await _refresh_completed_tree_evidence(database)
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id="7000",
            path="/private/movie.srt",
            remote_version=_source_version(
                "102", "/private/movie.srt", "movie.srt"
            ),
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
        operation_service, database.session_factory, transport, sleep=_noop_sleep
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
    await _refresh_completed_tree_evidence(database)
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id="7000",
            path="/private/movie.srt",
            remote_version=_source_version(
                "102", "/private/movie.srt", "movie.srt"
            ),
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
        operation_service, database.session_factory, transport, sleep=_noop_sleep
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
