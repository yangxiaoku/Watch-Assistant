import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    EmptyDirectoryCleanupPlan,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    ManagedDirectoryOwnership,
    MediaLibrary,
)
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupStatus,
)
from watch_assistant.services.empty_directory_cleanup_plan import (
    EmptyDirectoryCleanupPlanError,
    EmptyDirectoryCleanupPlanService,
)
from watch_assistant.services.strm_operations import StrmOperationService


async def _database(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'empty-cleanup.db'}")
    await initialize_database(database.engine)
    return database


async def _seed(database, *, snapshot_revision=1, run_id="run-1"):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(MediaLibrary(
            id="library-1",
            name="媒体库",
            root_directory_id="100",
            enabled=True,
            scope_verified=True,
            revision=1,
            created_at=now,
        ))
        await session.flush()
        run = LibraryScanRun(
            id=run_id,
            library_id="library-1",
            root_directory_id="100",
            idempotency_key=run_id,
            state="completed",
            complete=True,
            snapshot_revision=snapshot_revision,
            pages_read=1,
            items_seen=2,
            expected_total=2,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        await session.flush()
        session.add_all([
            LibraryScanEntry(scan_run_id=run_id, object_type="directory", object_id="200", parent_id="100", name="受管来源", path="受管来源", is_directory=True, size_bytes=None, modified_at=None),
            LibraryScanEntry(scan_run_id=run_id, object_type="directory", object_id="300", parent_id="200", name="空目录", path="受管来源/空目录", is_directory=True, size_bytes=None, modified_at=None),
        ])
        session.add(
            LibraryScanCheckpoint(
                scan_run_id=run_id,
                page=1,
                items_seen=2,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"100": 1, "200": 1, "300": 0},
                        "expected_total": 2,
                        "pending": [],
                        "visited": ["100", "200", "300"],
                    }
                ),
            )
        )
        session.add(
            ManagedDirectoryOwnership(
                directory_id="300",
                library_id="library-1",
                parent_directory_id="200",
                name="空目录",
                relative_path="受管来源/空目录",
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_directory_plan_requires_latest_complete_scan_and_is_idempotent(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        assert plan.candidate_count == 1
        assert plan.executable_count == 1

        calls = []

        async def execute(candidate):
            calls.append(candidate["directory_id"])
            return EmptyDirectoryCleanupStatus.SUCCESS

        applied = await service.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-key-1",
            executor=execute,
            system_created_directory_ids=("300",),
        )
        assert applied.deleted == 1
        assert calls == ["300"]

        repeated = await service.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-key-1",
            executor=execute,
            system_created_directory_ids=("300",),
        )
        assert repeated.deleted == 1
        assert calls == ["300"]
        with pytest.raises(EmptyDirectoryCleanupPlanError, match="empty_cleanup_already_applied"):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-2",
                executor=execute,
                system_created_directory_ids=("300",),
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_plan_protects_configured_directories_and_rejects_stale_run(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        assert plan.candidate_count == 1

        now = datetime.now(UTC)
        async with database.session_factory() as session:
            session.add(LibraryScanRun(
                id="run-2",
                library_id="library-1",
                root_directory_id="100",
                idempotency_key="run-2",
                state="completed",
                complete=True,
                snapshot_revision=2,
                pages_read=1,
                items_seen=0,
                created_at=now,
                updated_at=now,
            ))
            await session.commit()
        with pytest.raises(EmptyDirectoryCleanupPlanError, match="source_snapshot_not_current"):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-1",
                executor=lambda _candidate: _success(),
                system_created_directory_ids=("300",),
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_plan_invalidates_after_uncertain_executor_failure(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        calls = []

        async def execute(_candidate):
            calls.append(True)
            raise RuntimeError("remote detail must stay private")

        with pytest.raises(EmptyDirectoryCleanupPlanError, match="empty_cleanup_failed"):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-1",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        current = await service.get_plan(plan.plan_id)
        assert current.status == "invalidated"
        assert calls == [True]
        with pytest.raises(EmptyDirectoryCleanupPlanError, match="empty_cleanup_not_reviewable"):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=current.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-2",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        assert calls == [True]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_plan_claim_is_compare_and_set_under_concurrent_confirmation(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        both_validated = asyncio.Event()
        validation_count = 0
        original_validate = service._validated_current_run

        async def delayed_validate(session, library_id, scan_run_id):
            nonlocal validation_count
            validation_count += 1
            if validation_count == 2:
                both_validated.set()
            await both_validated.wait()
            return await original_validate(session, library_id, scan_run_id)

        monkeypatch.setattr(service, "_validated_current_run", delayed_validate)
        calls = []

        async def execute(candidate):
            calls.append(candidate["directory_id"])
            return EmptyDirectoryCleanupStatus.SUCCESS

        first = asyncio.create_task(
            service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-concurrent-1",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        )
        second = asyncio.create_task(
            service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-concurrent-2",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        )
        results = await asyncio.gather(first, second, return_exceptions=True)
        successes = [result for result in results if not isinstance(result, Exception)]
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(successes) == 1
        assert len(errors) == 1
        assert getattr(errors[0], "code", None) in {
            "empty_cleanup_in_progress",
            "plan_revision_changed",
        }
        assert calls == ["300"]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_service_rejects_other_library_operation(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        plan_service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        operation_service = StrmOperationService(database.session_factory)
        operation = await operation_service.create(
            library_id="library-1",
            source_scan_run_id="run-1",
            kind=StrmOperationKind.FULL,
            idempotency_key="empty-cleanup-conflict",
        )
        await operation_service.start(operation.operation_id)
        calls = []

        async def execute(candidate):
            calls.append(candidate["directory_id"])
            return EmptyDirectoryCleanupStatus.SUCCESS

        with pytest.raises(
            EmptyDirectoryCleanupPlanError,
            match="strm_library_operation_conflict",
        ):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="empty-cleanup-conflict-apply",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        assert calls == []
        assert (await plan_service.get_plan(plan.plan_id)).status == "needs_review"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_service_rejects_wrong_operation_scope(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        plan_service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        async with database.session_factory() as session:
            session.add(
                StrmOperation(
                    id="strm_op_wrong_cleanup_scope",
                    library_id="library-1",
                    kind=StrmOperationKind.CLEANUP,
                    source_scan_run_id="other-run",
                    status=StrmOperationStatus.RUNNING,
                    lease_owner="owner-cleanup-scope",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.commit()

        calls = []

        async def execute(candidate):
            calls.append(candidate["directory_id"])
            return EmptyDirectoryCleanupStatus.SUCCESS

        async def lease_check():
            return True

        with pytest.raises(
            EmptyDirectoryCleanupPlanError,
            match="strm_operation_lease_lost",
        ):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="empty-cleanup-wrong-scope-apply",
                executor=execute,
                system_created_directory_ids=("300",),
                operation_id="strm_op_wrong_cleanup_scope",
                lease_check=lease_check,
            )
        assert calls == []
        assert (await plan_service.get_plan(plan.plan_id)).status == "needs_review"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_plan_blocks_missing_system_created_evidence(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        async with database.session_factory() as session:
            ownership = await session.get(ManagedDirectoryOwnership, "300")
            assert ownership is not None
            ownership.status = "recycled"
            await session.commit()
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
        )
        assert plan.candidate_count == 1
        assert plan.executable_count == 0
        assert plan.blocked_count == 1
        assert plan.candidates[0]["state"] == "blocked"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_empty_directory_plan_rejects_newer_unsettled_scan(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        now = datetime.now(UTC)
        async with database.session_factory() as session:
            session.add(
                LibraryScanRun(
                    id="run-pending",
                    library_id="library-1",
                    root_directory_id="100",
                    idempotency_key="run-pending-key",
                    state="queued",
                    complete=False,
                    snapshot_revision=None,
                    created_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                )
            )
            await session.commit()
        with pytest.raises(
            EmptyDirectoryCleanupPlanError, match="source_snapshot_not_current"
        ):
            await service.create_plan(
                library_id="library-1",
                source_scan_run_id="run-1",
                protected_directory_ids=("200",),
                system_created_directory_ids=("300",),
            )
    finally:
        await database.engine.dispose()


async def test_empty_directory_plan_rejects_malformed_complete_scan(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        async with database.session_factory() as session:
            checkpoint = await session.get(LibraryScanCheckpoint, "run-1")
            assert checkpoint is not None
            checkpoint.cursor_json = "{}"
            await session.commit()

        with pytest.raises(
            EmptyDirectoryCleanupPlanError, match="source_snapshot_not_ready"
        ):
            await EmptyDirectoryCleanupPlanService(
                database.session_factory
            ).create_plan(
                library_id="library-1",
                source_scan_run_id="run-1",
                protected_directory_ids=("200",),
                system_created_directory_ids=("300",),
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_stale_applying_plan_is_invalidated_without_executor_retry(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        now = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
        async with database.session_factory() as session:
            row = await session.get(EmptyDirectoryCleanupPlan, plan.plan_id)
            assert row is not None
            row.status = "applying"
            row.revision = plan.revision + 1
            row.updated_at = now - timedelta(hours=1)
            await session.commit()

        assert await service.recover_stale_applying(
            max_age=timedelta(minutes=30), now=now
        ) == 1
        current = await service.get_plan(plan.plan_id)
        assert current.status == "invalidated"
        assert current.revision == plan.revision + 2

        calls = []

        async def execute(_candidate):
            calls.append(True)
            return EmptyDirectoryCleanupStatus.SUCCESS

        with pytest.raises(EmptyDirectoryCleanupPlanError, match="empty_cleanup_not_reviewable"):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=current.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-after-crash",
                executor=execute,
                system_created_directory_ids=("300",),
                now=now,
            )
        assert calls == []
    finally:
        await database.engine.dispose()


async def test_stale_applying_plan_preserves_a_live_cleanup_operation(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        now = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
        operation_service = StrmOperationService(database.session_factory)
        operation = await operation_service.create(
            library_id="library-1",
            source_scan_run_id="run-1",
            kind=StrmOperationKind.CLEANUP,
            idempotency_key="live-cleanup-operation",
        )
        await operation_service.start(
            operation.operation_id,
            now=now - timedelta(minutes=1),
            lease_duration=timedelta(hours=1),
        )
        async with database.session_factory() as session:
            row = await session.get(EmptyDirectoryCleanupPlan, plan.plan_id)
            assert row is not None
            row.status = "applying"
            row.revision = plan.revision + 1
            row.updated_at = now - timedelta(hours=1)
            await session.commit()

        assert await service.recover_stale_applying(
            max_age=timedelta(minutes=30), now=now
        ) == 0
        async with database.session_factory() as session:
            current = await session.get(EmptyDirectoryCleanupPlan, plan.plan_id)
            assert current is not None
            assert current.status == "applying"
    finally:
        await database.engine.dispose()


@pytest.mark.parametrize(
    "commit_error",
    (IntegrityError, OperationalError),
    ids=("integrity", "operational"),
)
async def test_empty_directory_apply_reports_uncertain_after_final_commit_failure(
    tmp_path, monkeypatch, commit_error
):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        service = EmptyDirectoryCleanupPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id="library-1",
            source_scan_run_id="run-1",
            protected_directory_ids=("200",),
            system_created_directory_ids=("300",),
        )
        calls = []

        async def execute(_candidate):
            calls.append(True)
            return EmptyDirectoryCleanupStatus.SUCCESS

        original_commit = AsyncSession.commit
        commit_calls = 0

        async def fail_final_commit(session):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls >= 4:
                raise commit_error("forced commit failure", {}, RuntimeError("forced"))
            await original_commit(session)

        monkeypatch.setattr(AsyncSession, "commit", fail_final_commit)
        with pytest.raises(
            EmptyDirectoryCleanupPlanError, match="empty_cleanup_uncertain"
        ):
            await service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="uncertain-final-commit",
                executor=execute,
                system_created_directory_ids=("300",),
            )
        assert calls == [True]
    finally:
        await database.engine.dispose()


async def _success():
    return EmptyDirectoryCleanupStatus.SUCCESS
