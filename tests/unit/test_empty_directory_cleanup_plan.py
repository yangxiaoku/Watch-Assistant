import asyncio
from datetime import UTC, datetime

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupStatus,
)
from watch_assistant.services.empty_directory_cleanup_plan import (
    EmptyDirectoryCleanupPlanError,
    EmptyDirectoryCleanupPlanService,
)


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
        session.add(LibraryScanRun(
            id=run_id,
            library_id="library-1",
            root_directory_id="100",
            idempotency_key=run_id,
            state="completed",
            complete=True,
            snapshot_revision=snapshot_revision,
            pages_read=1,
            items_seen=3,
            created_at=now,
            updated_at=now,
        ))
        await session.flush()
        session.add_all([
            LibraryScanEntry(scan_run_id=run_id, object_type="directory", object_id="100", parent_id=None, name="根目录", path="", is_directory=True, size_bytes=None, modified_at=None),
            LibraryScanEntry(scan_run_id=run_id, object_type="directory", object_id="200", parent_id="100", name="受管来源", path="受管来源", is_directory=True, size_bytes=None, modified_at=None),
            LibraryScanEntry(scan_run_id=run_id, object_type="directory", object_id="300", parent_id="200", name="空目录", path="受管来源/空目录", is_directory=True, size_bytes=None, modified_at=None),
        ])
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


async def _success():
    return EmptyDirectoryCleanupStatus.SUCCESS
