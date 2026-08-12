from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_organization_operations import _database, _operation

from watch_assistant.db import create_database
from watch_assistant.models import (
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    OrganizationOperation,
    OrganizationOperationStatus,
    Resource,
    StrmOperation,
    Task,
    WorkflowStage,
)
from watch_assistant.schemas import (
    MediaType,
    RemoteObservation,
    RemoteStatus,
    WorkflowCreateRequest,
    WorkflowDiscoveryRequest,
    WorkflowStageName,
    WorkflowStagePatch,
    WorkflowStageStatus,
)
from watch_assistant.services.directory_dirty_worker import (
    DirectoryDirtyWorker,
    _dirty_idempotency_key,
    _dirty_operation_idempotency_key,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
)
from watch_assistant.services.organization_outbox import (
    DirectoryDirtyLease,
    DirectoryDirtyOutboxService,
)
from watch_assistant.services.strm_operations import (
    StrmOperationKind,
    StrmOperationService,
)
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.workflows import WorkflowService


async def _claimed(database, *, workflow_id=None):
    service = OrganizationOperationService(database.session_factory)
    operation = await _operation(database)
    if workflow_id is not None:
        async with database.session_factory() as session:
            session.add(
                Resource(
                    id="dirty-resource",
                    kind="magnet",
                    canonical_key="magnet:dirty-resource",
                    encrypted_url="encrypted-dirty-resource",
                    name="Dirty worker resource",
                    source="test",
                    captured_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await session.commit()
        workflow_service = WorkflowService(database.session_factory)
        await workflow_service.record_discovery(
            workflow_id,
            WorkflowDiscoveryRequest(resource_id="dirty-resource"),
        )
        for stage_name in (
            WorkflowStageName.INSPECTION,
            WorkflowStageName.APPROVAL,
        ):
            await workflow_service.patch_stage(
                workflow_id,
                stage_name,
                WorkflowStagePatch(status=WorkflowStageStatus.SUCCEEDED),
            )
        task, _ = await TaskService(database.session_factory).create(
            "dirty-resource", workflow_id=workflow_id
        )
        async with database.session_factory() as session:
            row = await session.get(OrganizationOperation, operation.operation_id)
            row.workflow_id = workflow_id
            stored_task = await session.get(Task, task.id)
            assert stored_task is not None
            stored_task.remote_ref = "dirty-available"
            await session.commit()

        class AvailableAdapter:
            async def get_status_for_task(
                self, remote_ref: str, *, target_directory_id: str | None
            ):
                assert remote_ref == "dirty-available"
                return RemoteObservation(
                    status=RemoteStatus.AVAILABLE,
                    file_id="101",
                    parent_id="7",
                    is_directory=False,
                )

        await TaskService(database.session_factory).reconcile(
            task.id, AvailableAdapter()
        )
    lease = await service.claim(operation.operation_id, expected_revision=1)
    return service, operation, lease


class _FakeIndex:
    def __init__(self):
        self.calls = []

    async def scan_tree(self, key):
        self.calls.append(key)
        return SimpleNamespace(complete=True, run_id="scan-dirty-result")


class _FakeStrm:
    def __init__(self):
        self.calls = []
        self.lease_was_active = False

    async def incremental(self, library_id, **kwargs):
        self.calls.append((library_id, kwargs))
        lease_check = kwargs.get("lease_check")
        if callable(lease_check):
            self.lease_was_active = await lease_check()
        progress_callback = kwargs.get("progress_callback")
        if callable(progress_callback):
            await progress_callback(
                SimpleNamespace(
                    generated=1,
                    unchanged=0,
                    skipped=0,
                    failed=0,
                    retired=0,
                )
            )
        return SimpleNamespace(
            generated=1,
            unchanged=0,
            skipped=0,
            failed=0,
            retired=0,
        )


class _FailedStrm(_FakeStrm):
    async def incremental(self, library_id, **kwargs):
        self.calls.append((library_id, kwargs))
        return SimpleNamespace(
            generated=0,
            unchanged=0,
            skipped=0,
            failed=1,
            retired=0,
        )


def test_dirty_retry_idempotency_keys_stay_stable_for_one_generation():
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    first = DirectoryDirtyLease(
        event_id="evt-one",
        operation_id="org-one",
        directory_id="dir-one",
        lease_token="lease-one",
        attempts=1,
        lease_expires_at=expires_at,
        queue_id="gen-one",
        generation=4,
    )
    retry = DirectoryDirtyLease(
        event_id="evt-one",
        operation_id="org-one",
        directory_id="dir-one",
        lease_token="lease-two",
        attempts=2,
        lease_expires_at=expires_at,
        queue_id="gen-one",
        generation=4,
    )

    assert _dirty_idempotency_key(first) == _dirty_idempotency_key(retry)
    assert _dirty_operation_idempotency_key(
        first, "scan-one"
    ) == _dirty_operation_idempotency_key(retry, "scan-one")


class _OrganizationSettings:
    strm_linkage_enabled = False
    cleanup_empty_directories = False


class _Settings:
    async def get_organization(self):
        return _OrganizationSettings()


@pytest.mark.asyncio
async def test_dirty_worker_consumes_event_and_preserves_cleanup_gate(tmp_path: Path):
    database = await _database(tmp_path)
    workflow = await WorkflowService(database.session_factory).create(
        WorkflowCreateRequest(media_type=MediaType.MOVIE, tmdb_id=1)
    )
    service, operation, lease = await _claimed(database, workflow_id=workflow.id)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    async with database.session_factory() as session:
        event = await session.scalar(
            select(DirectoryDirtyEvent)
            .where(DirectoryDirtyEvent.directory_id == "7000")
            .order_by(DirectoryDirtyEvent.id)
        )
        assert event is not None

    index = _FakeIndex()
    strm = _FakeStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        strm,
        lambda library_id, root_id: index,
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        cleanup_enabled=False,
    )
    assert await worker.run_once()
    assert len(index.calls) == 1
    assert len(strm.calls) == 1
    assert strm.calls[0][1]["retire_removed"] is False
    assert strm.calls[0][1]["operation_id"].startswith("strm_op_")
    assert strm.lease_was_active is True
    async with database.session_factory() as session:
        current = await session.get(DirectoryDirtyEvent, event.id)
        assert current is not None
        assert current.status == "consumed"
        assert current.error_code is None
        operation_row = await session.scalar(
            select(StrmOperation).where(
                StrmOperation.library_id == "library-1",
                StrmOperation.kind == StrmOperationKind.INCREMENTAL,
            )
        )
        assert operation_row is not None
        assert operation_row.status.value == "succeeded"
        assert operation_row.generated == 1
        assert operation_row.lease_owner is None
    workflow_state = await WorkflowService(database.session_factory).get(workflow.id)
    strm_stage = next(
        stage for stage in workflow_state.stages if stage.stage is WorkflowStageName.STRM
    )
    assert strm_stage.status is WorkflowStageStatus.SUCCEEDED
    assert strm_stage.child_type == "strm_dirty_generation"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_retries_when_strm_operation_is_not_successful(tmp_path: Path):
    database = await _database(tmp_path)
    workflow = await WorkflowService(database.session_factory).create(
        WorkflowCreateRequest(media_type=MediaType.MOVIE, tmdb_id=1)
    )
    service, operation, lease = await _claimed(database, workflow_id=workflow.id)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    failed_strm = _FailedStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        failed_strm,
        lambda _library_id, _root_id: _FakeIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
    )

    assert await worker.run_once()
    async with database.session_factory() as session:
        event = await session.scalar(
            select(DirectoryDirtyEvent).where(
                DirectoryDirtyEvent.directory_id == "7000"
            )
        )
        assert event is not None
        assert event.status == "pending"
        assert event.error_code == "strm_incremental_failed"
        operation_row = await session.scalar(
            select(StrmOperation).where(
                StrmOperation.library_id == "library-1",
                StrmOperation.kind == StrmOperationKind.INCREMENTAL,
            )
        )
        assert operation_row is not None
        assert operation_row.status.value == "failed"

    workflow_state = await WorkflowService(database.session_factory).get(workflow.id)
    strm_stage = next(
        stage for stage in workflow_state.stages if stage.stage is WorkflowStageName.STRM
    )
    assert strm_stage.status is WorkflowStageStatus.WAITING_EXTERNAL
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_claim_is_mutually_exclusive_with_api_operation(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    other_database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}"
    )
    try:
        service, operation, lease = await _claimed(database)
        await service.finish(
            operation.operation_id,
            expected_revision=lease.revision,
            lease_token=lease.lease_token,
            status=OrganizationOperationStatus.ORGANIZED,
            source_directory_id="7000",
            target_directory_id="8000",
        )
        api_operations = StrmOperationService(database.session_factory)
        api_operation = await api_operations.create(
            library_id="library-1",
            source_scan_run_id="api-scan",
            kind=StrmOperationKind.FULL,
            idempotency_key="api-running-operation",
        )
        api_running = await api_operations.start(api_operation.operation_id)
        assert api_running.status == "running"

        index = _FakeIndex()
        strm = _FakeStrm()
        worker = DirectoryDirtyWorker(
            other_database.session_factory,
            strm,
            lambda _library_id, _root_id: index,
            output_root=tmp_path / "strm",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert await worker.run_once()
        assert len(index.calls) == 1
        assert strm.calls == []
        async with database.session_factory() as session:
            event = await session.scalar(
                select(DirectoryDirtyEvent).where(
                    DirectoryDirtyEvent.directory_id == "7000"
                )
            )
            assert event is not None
            assert event.status == "pending"
            assert event.error_code == "strm_operation_in_progress"
            queued = list(
                (
                    await session.scalars(
                        select(StrmOperation).where(
                            StrmOperation.library_id == "library-1",
                            StrmOperation.kind == StrmOperationKind.INCREMENTAL,
                        )
                    )
                ).all()
            )
            assert len(queued) == 1
            assert queued[0].status.value == "queued"
        assert (await api_operations.get(api_operation.operation_id)).status == "running"
    finally:
        await other_database.engine.dispose()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_retries_incomplete_scan(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    class _IncompleteIndex:
        async def scan_tree(self, _key):
            return SimpleNamespace(complete=False, run_id="scan-incomplete")

    worker = DirectoryDirtyWorker(
        database.session_factory,
        _FakeStrm(),
        lambda _library_id, _root_id: _IncompleteIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
    )
    assert await worker.run_once()
    async with database.session_factory() as session:
        row = await session.scalar(
            select(DirectoryDirtyEvent).where(DirectoryDirtyEvent.directory_id == "7000")
        )
        assert row is not None
        assert row.status == "pending"
        assert row.error_code == "scan_incomplete"
        assert row.attempts == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_does_not_write_workflow_after_retry_lease_is_replaced(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    workflow = await WorkflowService(database.session_factory).create(
        WorkflowCreateRequest(media_type=MediaType.MOVIE, tmdb_id=1)
    )
    service, operation, lease = await _claimed(database, workflow_id=workflow.id)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    class _LeaseReplacedOutbox(DirectoryDirtyOutboxService):
        async def retry(self, session_factory, current_lease, **_kwargs):
            now = datetime.now(UTC) + timedelta(minutes=5)
            async with session_factory() as session:
                generation = await session.get(
                    DirectoryDirtyGeneration, current_lease.queue_id
                )
                event = await session.get(DirectoryDirtyEvent, current_lease.event_id)
                stage = await session.scalar(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow.id,
                        WorkflowStage.stage == WorkflowStageName.STRM,
                    )
                )
                assert generation is not None
                assert event is not None
                assert stage is not None
                generation.lease_token = "new-lease-token"
                generation.lease_expires_at = now
                event.lease_token = "new-lease-token"
                event.lease_expires_at = now
                stage.status = WorkflowStageStatus.RUNNING
                stage.reason = "new_worker_started"
                stage.updated_at = now
                await session.commit()
            return False

    class _IncompleteIndex:
        async def scan_tree(self, _key):
            return SimpleNamespace(complete=False, run_id="scan-incomplete")

    worker = DirectoryDirtyWorker(
        database.session_factory,
        _FakeStrm(),
        lambda _library_id, _root_id: _IncompleteIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        outbox=_LeaseReplacedOutbox(),
    )
    assert await worker.run_once()
    workflow_state = await WorkflowService(database.session_factory).get(workflow.id)
    strm_stage = next(
        stage for stage in workflow_state.stages if stage.stage is WorkflowStageName.STRM
    )
    assert strm_stage.status is WorkflowStageStatus.RUNNING
    assert strm_stage.reason == "new_worker_started"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_does_not_finalize_after_dirty_lease_is_replaced(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    class _LeaseReplacingStrm(_FakeStrm):
        async def incremental(self, library_id, **kwargs):
            self.calls.append((library_id, kwargs))
            replacement = "replacement-dirty-lease"
            async with database.session_factory() as session:
                generation = await session.scalar(
                    select(DirectoryDirtyGeneration).where(
                        DirectoryDirtyGeneration.status == "running"
                    )
                )
                event = await session.scalar(
                    select(DirectoryDirtyEvent).where(
                        DirectoryDirtyEvent.status == "running"
                    )
                )
                assert generation is not None
                assert event is not None
                generation.lease_token = replacement
                generation.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                event.lease_token = replacement
                event.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                await session.commit()
            return SimpleNamespace(
                generated=1,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=0,
            )

    strm = _LeaseReplacingStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        strm,
        lambda _library_id, _root_id: _FakeIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
    )

    assert await worker.run_once()
    assert len(strm.calls) == 1
    async with database.session_factory() as session:
        dirty_event = await session.scalar(
            select(DirectoryDirtyEvent).where(
                DirectoryDirtyEvent.status == "running"
            )
        )
        strm_operation = await session.scalar(
            select(StrmOperation).where(
                StrmOperation.library_id == "library-1",
                StrmOperation.kind == StrmOperationKind.INCREMENTAL,
            )
        )
        assert dirty_event is not None
        assert dirty_event.status == "running"
        assert dirty_event.lease_token == "replacement-dirty-lease"
        assert strm_operation is not None
        assert strm_operation.status.value == "running"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_fences_strm_terminal_state_with_dirty_lease(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )

    class _LeaseReplacingOperations(StrmOperationService):
        async def complete(self, operation_id, **kwargs):
            replacement = "replacement-before-terminal-fence"
            async with database.session_factory() as session:
                generation = await session.scalar(
                    select(DirectoryDirtyGeneration).where(
                        DirectoryDirtyGeneration.status == "running"
                    )
                )
                event = await session.scalar(
                    select(DirectoryDirtyEvent).where(
                        DirectoryDirtyEvent.status == "running"
                    )
                )
                assert generation is not None
                assert event is not None
                generation.lease_token = replacement
                generation.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                event.lease_token = replacement
                event.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                await session.commit()
            return await super().complete(operation_id, **kwargs)

    worker = DirectoryDirtyWorker(
        database.session_factory,
        _FakeStrm(),
        lambda _library_id, _root_id: _FakeIndex(),
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        operation_service=_LeaseReplacingOperations(database.session_factory),
    )

    assert await worker.run_once()
    async with database.session_factory() as session:
        dirty_event = await session.scalar(
            select(DirectoryDirtyEvent).where(
                DirectoryDirtyEvent.status == "running"
            )
        )
        strm_operation = await session.scalar(
            select(StrmOperation).where(
                StrmOperation.library_id == "library-1",
                StrmOperation.kind == StrmOperationKind.INCREMENTAL,
            )
        )
        assert dirty_event is not None
        assert dirty_event.lease_token == "replacement-before-terminal-fence"
        assert strm_operation is not None
        assert strm_operation.status.value == "running"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_dirty_worker_consumes_without_scan_when_linkage_is_disabled(tmp_path: Path):
    database = await _database(tmp_path)
    service, operation, lease = await _claimed(database)
    await service.finish(
        operation.operation_id,
        expected_revision=lease.revision,
        lease_token=lease.lease_token,
        status=OrganizationOperationStatus.ORGANIZED,
        source_directory_id="7000",
        target_directory_id="8000",
    )
    index = _FakeIndex()
    strm = _FakeStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        strm,
        lambda _library_id, _root_id: index,
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        settings_service=_Settings(),
    )

    assert await worker.run_once()
    assert await worker.run_once()
    assert index.calls == []
    assert strm.calls == []
    async with database.session_factory() as session:
        event = await session.scalar(
            select(DirectoryDirtyEvent).where(DirectoryDirtyEvent.directory_id == "7000")
        )
        assert event is not None
        assert event.status == "consumed"
        assert event.error_code == "strm_linkage_disabled"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_run_forever_survives_run_once_exception(tmp_path):
    """M16:run_once 抛异常时 run_forever 不得让消费循环死亡;
    异常被隔离,循环继续等待直至 stop_event。"""
    import asyncio

    database = await _database(tmp_path)
    index = _FakeIndex()
    strm = _FakeStrm()
    worker = DirectoryDirtyWorker(
        database.session_factory,
        strm,
        lambda library_id, root_id: index,
        output_root=tmp_path / "strm",
        playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        cleanup_enabled=False,
        poll_interval_seconds=0.01,
    )
    calls = {"count": 0}

    async def _explode():
        calls["count"] += 1
        raise RuntimeError("transient-failure")

    worker.run_once = _explode  # type: ignore[method-assign]
    stop = asyncio.Event()

    async def _stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.gather(_stop_later(), task)
    assert task.exception() is None, "异常不得逃逸出 run_forever"
    assert calls["count"] >= 1
    await database.engine.dispose()
