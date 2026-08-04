import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import watch_assistant.services.strm_cleanup_plan as strm_cleanup_plan_module
import watch_assistant.services.strm_manifest as strm_manifest_module
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmCleanupPlan,
    StrmManifestEntry,
)
from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus
from watch_assistant.services.strm_cleanup_plan import (
    StrmCleanupPlanError,
    StrmCleanupPlanService,
)
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    StrmManifestService,
    _FileMutation,
    _LeaseFence,
)
from watch_assistant.services.strm_verification import (
    StrmVerificationError,
    StrmVerificationService,
)


async def _database(tmp_path: Path, *, include_second_video: bool = False):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-strm",
                name="STRM",
                root_directory_id="root-strm",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        run = LibraryScanRun(
            id="scan-strm",
            library_id="library-strm",
            root_directory_id="root-strm",
            idempotency_key="scan-key",
            state="completed",
            complete=True,
            snapshot_revision=1,
            scan_mode="tree",
            pages_read=1,
            items_seen=2,
            expected_total=2,
        )
        session.add(run)
        await session.flush()
        entries = [
            LibraryScanEntry(
                scan_run_id="scan-strm",
                object_type="file",
                object_id="100",
                parent_id="root-strm",
                name="Episode.mkv",
                path="Show/Episode.mkv",
                is_directory=False,
                size_bytes=100,
                pickcode="protected-episode-pickcode",
            ),
            LibraryScanEntry(
                scan_run_id="scan-strm",
                object_type="file",
                object_id="101",
                parent_id="root-strm",
                name="notes.txt",
                path="notes.txt",
                is_directory=False,
                size_bytes=10,
            ),
        ]
        if include_second_video:
            entries.append(
                LibraryScanEntry(
                    scan_run_id="scan-strm",
                    object_type="file",
                    object_id="102",
                    parent_id="root-strm",
                    name="Old.mkv",
                    path="Show/Old.mkv",
                    is_directory=False,
                    size_bytes=200,
                )
            )
        run.items_seen = len(entries)
        run.expected_total = len(entries)
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-strm",
                page=1,
                items_seen=len(entries),
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-strm": len(entries)},
                        "expected_total": len(entries),
                        "pending": [],
                        "visited": ["root-strm"],
                    }
                ),
            )
        )
        session.add_all(entries)
        await session.commit()
    return database


async def test_generation_is_bounded_to_complete_scan_and_idempotent(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        first = await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        second = await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        items, total = await service.list_current("library-strm")

        assert first.generated == 1
        assert first.skipped == 1
        assert second.unchanged == 1
        assert total == 1
        assert items[0].local_relative_path == "Show/Episode.strm"
        content = (tmp_path / "output" / "Show" / "Episode.strm").read_text()
        assert (
            content
            == f"http://127.0.0.1:8115/api/v1/strm/play/{items[0].manifest_id}\n"
        )
        async with database.session_factory() as session:
            manifest = await session.get(StrmManifestEntry, items[0].manifest_id)
        assert manifest is not None
        assert manifest.pickcode == "protected-episode-pickcode"
        assert "pickcode" not in content.lower()
        assert "token" not in content.lower()
        assert "cookie" not in content.lower()
    finally:
        await database.engine.dispose()


async def test_manifest_generation_refuses_write_after_lease_loss(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)

        async def lease_check() -> bool:
            return False

        with pytest.raises(StrmManifestError, match="strm_operation_lease_lost"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                lease_check=lease_check,
                operation_id="strm_op_lost_lease",
            )
        assert not (tmp_path / "output" / "Show" / "Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_manifest_operation_id_requires_lease_check(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        with pytest.raises(StrmManifestError, match="strm_operation_lease_required"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                operation_id="strm_op_without_lease_check",
            )
        assert not (tmp_path / "output" / "Show" / "Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_successful_manifest_commit_disarms_file_compensation(
    tmp_path: Path, monkeypatch
):
    async def commit_without_failure(_session, _fence):
        return None

    monkeypatch.setattr(
        strm_manifest_module, "_commit_fenced", commit_without_failure
    )
    mutations = [
        _FileMutation(
            root=tmp_path,
            relative_path="Episode.strm",
            before=b"old\n",
            after=b"new\n",
        )
    ]

    await StrmManifestService(None)._commit_entry(
        None, _LeaseFence(None, None), None, mutations
    )

    assert mutations == []


async def test_manifest_commit_cancel_after_durable_commit_keeps_success(
    tmp_path: Path, monkeypatch
):
    database = await _database(tmp_path)
    try:
        async def commit_then_cancel(session, _fence):
            await session.commit()
            raise asyncio.CancelledError

        monkeypatch.setattr(
            strm_manifest_module, "_commit_fenced", commit_then_cancel
        )
        summary = await StrmManifestService(database.session_factory).generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.generated == 1
        assert (tmp_path / "output/Show/Episode.strm").exists()
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is not None
            assert manifest.status == "verified"
    finally:
        await database.engine.dispose()


async def test_manifest_durable_fence_rejection_restores_file_and_manifest(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        fence_calls = 0

        async def durable_fence(_session: AsyncSession) -> bool:
            nonlocal fence_calls
            fence_calls += 1
            return fence_calls < 5

        with pytest.raises(StrmManifestError, match="strm_operation_lease_lost"):
            await StrmManifestService(database.session_factory).generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                durable_fence=durable_fence,
            )

        assert fence_calls == 5
        assert not (tmp_path / "output/Show/Episode.strm").exists()
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is None
    finally:
        await database.engine.dispose()


async def test_generation_requires_complete_current_scan(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        async with database.session_factory() as session:
            session.add(
                LibraryScanRun(
                    id="scan-incomplete",
                    library_id="library-strm",
                    root_directory_id="root-strm",
                    idempotency_key="scan-incomplete-key",
                    state="completed",
                    complete=False,
                    snapshot_revision=2,
                )
            )
            session.add(
                LibraryScanRun(
                    id="scan-current",
                    library_id="library-strm",
                    root_directory_id="root-strm",
                    idempotency_key="scan-current-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=2,
                )
            )
            await session.commit()

        service = StrmManifestService(database.session_factory)
        with pytest.raises(StrmManifestError, match="source_snapshot_not_ready"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-incomplete",
                output_root=tmp_path / "incomplete-output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmManifestError, match="source_snapshot_not_current"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "stale-output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()


async def test_strm_consumers_reject_ambiguous_current_snapshot_revision(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    try:
        async with database.session_factory() as session:
            source = await session.get(LibraryScanRun, "scan-strm")
            checkpoint = await session.get(LibraryScanCheckpoint, "scan-strm")
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == "scan-strm"
                        )
                    )
                ).all()
            )
            assert source is not None
            assert checkpoint is not None
            duplicate = LibraryScanRun(
                id="scan-strm-duplicate",
                library_id=source.library_id,
                root_directory_id=source.root_directory_id,
                idempotency_key="scan-strm-duplicate-key",
                scan_mode=source.scan_mode,
                max_directories=source.max_directories,
                state=source.state,
                complete=source.complete,
                snapshot_revision=source.snapshot_revision,
                expected_page_count=source.expected_page_count,
                expected_total=source.expected_total,
                pages_read=source.pages_read,
                items_seen=source.items_seen,
            )
            session.add(duplicate)
            await session.flush()
            session.add(
                LibraryScanCheckpoint(
                    scan_run_id=duplicate.id,
                    page=checkpoint.page,
                    items_seen=checkpoint.items_seen,
                    cursor_json=checkpoint.cursor_json,
                )
            )
            session.add_all(
                [
                    LibraryScanEntry(
                        scan_run_id=duplicate.id,
                        object_type=entry.object_type,
                        object_id=entry.object_id,
                        parent_id=entry.parent_id,
                        name=entry.name,
                        path=entry.path,
                        pickcode=entry.pickcode,
                        is_directory=entry.is_directory,
                        size_bytes=entry.size_bytes,
                        modified_at=entry.modified_at,
                    )
                    for entry in entries
                ]
            )
            await session.commit()

        manifest = StrmManifestService(database.session_factory)
        with pytest.raises(StrmManifestError, match="source_snapshot_not_current"):
            await manifest.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=output_root,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmManifestError, match="source_snapshot_not_current"):
            await manifest.incremental(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=output_root,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmCleanupPlanError, match="source_snapshot_not_current"):
            await StrmCleanupPlanService(database.session_factory).create_plan(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                output_root=output_root,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmVerificationError, match="source_snapshot_not_current"):
            await StrmVerificationService(database.session_factory).verify(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                output_root=output_root,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()


async def test_generation_rejects_complete_snapshot_with_broken_tree_evidence(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        async with database.session_factory() as session:
            entry = await session.get(LibraryScanEntry, ("scan-strm", "file", "100"))
            assert entry is not None
            entry.parent_id = "outside-root"
            await session.commit()

        service = StrmManifestService(database.session_factory)
        with pytest.raises(StrmManifestError, match="source_snapshot_not_ready"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        assert not (tmp_path / "output" / "Show" / "Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_all_strm_snapshot_consumers_reject_invalid_checkpoint(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        async with database.session_factory() as session:
            checkpoint = await session.get(LibraryScanCheckpoint, "scan-strm")
            assert checkpoint is not None
            checkpoint.cursor_json = "{}"
            await session.commit()

        manifest = StrmManifestService(database.session_factory)
        with pytest.raises(StrmManifestError, match="source_snapshot_not_ready"):
            await manifest.incremental(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmCleanupPlanError, match="source_snapshot_not_ready"):
            await StrmCleanupPlanService(database.session_factory).create_plan(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmVerificationError, match="source_snapshot_not_ready"):
            await StrmVerificationService(database.session_factory).verify(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()


async def test_strm_operations_block_newer_unsettled_scan_for_every_reconciliation_path(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest = StrmManifestService(database.session_factory)
        await manifest.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        now = datetime.now(UTC)
        async with database.session_factory() as session:
            session.add(
                LibraryScanRun(
                    id="scan-pending",
                    library_id="library-strm",
                    root_directory_id="root-strm",
                    idempotency_key="scan-pending-key",
                    state="queued",
                    complete=False,
                    snapshot_revision=None,
                    created_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                )
            )
            await session.commit()

        with pytest.raises(StrmManifestError, match="source_snapshot_not_current"):
            await manifest.generate(
                "library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmManifestError, match="source_snapshot_not_current"):
            await manifest.incremental(
                "library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        cleanup = StrmCleanupPlanService(database.session_factory)
        with pytest.raises(StrmCleanupPlanError, match="source_snapshot_not_current"):
            await cleanup.create_plan(
                library_id="library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        with pytest.raises(StrmVerificationError, match="source_snapshot_not_current"):
            await StrmVerificationService(database.session_factory).verify(
                library_id="library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()


async def test_manifest_terminal_commit_rejects_newer_complete_snapshot(
    tmp_path: Path, monkeypatch
):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        original_content = target.read_bytes()
        original_commit = strm_manifest_module._commit_fenced

        async def racing_commit(session, fence):
            session.add(
                LibraryScanRun(
                    id="scan-newer-final",
                    library_id="library-strm",
                    root_directory_id="root-strm",
                    idempotency_key="scan-newer-final-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=2,
                )
            )
            await session.flush()
            await original_commit(session, fence)

        monkeypatch.setattr(strm_manifest_module, "_commit_fenced", racing_commit)
        summary = await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.failed == 1
        assert target.read_bytes() == original_content
        items, total = await service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_generation_enforces_managed_root_and_rejects_symlink_path(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    managed_root = tmp_path / "managed"
    outside_root = tmp_path / "outside"
    try:
        service = StrmManifestService(
            database.session_factory,
            managed_output_roots=(managed_root,),
        )
        with pytest.raises(StrmManifestError, match="output_root_not_allowed"):
            await service.generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "outside-allowlist",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        managed_root.mkdir()
        outside_root.mkdir()
        symlinked_directory = managed_root / "Show"
        try:
            symlinked_directory.symlink_to(outside_root, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable in this environment")

        summary = await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=managed_root,
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        assert summary.generated == 0
        assert summary.failed == 1
        assert not (outside_root / "Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_generation_rejects_local_path_collision(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        async with database.session_factory() as session:
            entry = await session.scalar(
                select(LibraryScanEntry).where(LibraryScanEntry.object_id == "101")
            )
            assert entry is not None
            entry.name = "Episode.mp4"
            entry.path = "Show/Episode.mp4"
            await session.commit()

        service = StrmManifestService(database.session_factory)
        summary = await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.generated == 1
        assert summary.failed == 1
        assert (tmp_path / "output/Show/Episode.strm").exists()
        items, total = await service.list_current("library-strm")
        assert total == 1
        assert items[0].cloud_file_id == "100"
    finally:
        await database.engine.dispose()


async def test_generation_does_not_overwrite_unmanaged_existing_strm(tmp_path: Path):
    database = await _database(tmp_path)
    target = tmp_path / "output/Show/Episode.strm"
    original_content = b"user-owned content\n"
    target.parent.mkdir(parents=True)
    target.write_bytes(original_content)
    try:
        summary = await StrmManifestService(database.session_factory).generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.generated == 0
        assert summary.failed == 1
        assert target.read_bytes() == original_content
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is None
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_is_persistent_read_only_and_idempotent(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        service = StrmCleanupPlanService(database.session_factory)
        first = await service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        second = await service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert first.plan_id == second.plan_id
        assert first.candidate_count == 1
        assert first.executable_count == 1
        assert first.blocked_count == 0
        assert (tmp_path / "output/Show/Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_marks_user_modified_strm_blocked(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        (tmp_path / "output/Show/Episode.strm").write_text("user content\n")
        await _add_removed_episode_scan(database)
        plan = await StrmCleanupPlanService(database.session_factory).create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert plan.candidate_count == 1
        assert plan.executable_count == 0
        assert plan.blocked_count == 1
        assert (tmp_path / "output/Show/Episode.strm").read_text() == "user content\n"
    finally:
        await database.engine.dispose()


async def test_incremental_does_not_overwrite_unmanaged_renamed_target(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode-renamed.strm"
        original_content = b"user-owned content\n"
        target.write_bytes(original_content)
        await _add_changed_scan(database)

        summary = await service.incremental(
            "library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.generated == 1
        assert summary.failed == 1
        assert target.read_bytes() == original_content
        assert (tmp_path / "output/Show/Episode.strm").exists()
        items, total = await service.list_current("library-strm")
        assert total == 2
        renamed = next(item for item in items if item.cloud_file_id == "100")
        assert renamed.local_relative_path == "Show/Episode.strm"
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_apply_requires_digest_and_retires_only_managed_file(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        result = await plan_service.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-apply-one",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        repeated = await plan_service.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=result.plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-apply-one",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert result.retired == 1
        assert result.plan.status == "applied"
        assert repeated.retired == 1
        assert not (tmp_path / "output/Show/Episode.strm").exists()
        items, total = await manifest_service.list_current("library-strm")
        assert total == 0
        assert items == ()
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_apply_rejects_manifest_path_changed_after_review(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        expected = (tmp_path / "output/Show/Episode.strm").read_bytes()
        relocated = tmp_path / "output/Relocated/Episode.strm"
        relocated.parent.mkdir(parents=True)
        relocated.write_bytes(expected)
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is not None
            manifest.local_relative_path = "Relocated/Episode.strm"
            await session.commit()

        with pytest.raises(StrmCleanupPlanError, match="cleanup_plan_changed"):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-path-changed",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        assert (tmp_path / "output/Show/Episode.strm").read_bytes() == expected
        assert relocated.read_bytes() == expected
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].local_relative_path == "Relocated/Episode.strm"
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_apply_fails_closed_when_terminal_idempotency_key_is_missing(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        async with database.session_factory() as session:
            row = await session.get(StrmCleanupPlan, plan.plan_id)
            assert row is not None
            row.status = "applied"
            row.revision += 1
            row.applied_idempotency_key = None
            row.applied_retired = 1
            await session.commit()

        with pytest.raises(
            StrmCleanupPlanError, match="cleanup_plan_already_applied"
        ):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-key-retry",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_terminal_commit_uses_revision_cas(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        async with database.session_factory() as first_session:
            first_plan = await first_session.get(StrmCleanupPlan, plan.plan_id)
            assert first_plan is not None
            first_plan.status = "applied"
            first_plan.revision += 1
            first_plan.applied_idempotency_key = "cleanup-cas-first"
            first_plan.applied_retired = 1
            await plan_service._commit_plan(
                first_session,
                _LeaseFence(None, None),
                first_plan,
                idempotency_key="cleanup-cas-first",
                retired=1,
                mutations=[],
            )

        async with database.session_factory() as stale_session:
            stale_plan = await stale_session.get(StrmCleanupPlan, plan.plan_id)
            assert stale_plan is not None
            stale_plan.status = "applied"
            stale_plan.revision += 1
            stale_plan.applied_idempotency_key = "cleanup-cas-second"
            stale_plan.applied_retired = 1
            with pytest.raises(StrmCleanupPlanError, match="cleanup_plan_changed"):
                await plan_service._commit_plan(
                    stale_session,
                    _LeaseFence(None, None),
                    stale_plan,
                    idempotency_key="cleanup-cas-second",
                    retired=1,
                    mutations=[],
                )
            await stale_session.rollback()

        current = await plan_service.get_plan(plan.plan_id)
        assert current.status == "applied"
        async with database.session_factory() as session:
            row = await session.get(StrmCleanupPlan, plan.plan_id)
            assert row is not None
            assert row.applied_idempotency_key == "cleanup-cas-first"
            assert row.revision == plan.revision + 1
    finally:
        await database.engine.dispose()


async def test_cleanup_terminal_commit_rejects_newer_complete_snapshot(
    tmp_path: Path, monkeypatch
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        original_content = target.read_bytes()
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        original_commit = strm_cleanup_plan_module._commit_fenced

        async def racing_commit(session, fence):
            session.add(
                LibraryScanRun(
                    id="scan-cleanup-newer-final",
                    library_id="library-strm",
                    root_directory_id="root-strm",
                    idempotency_key="scan-cleanup-newer-final-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=3,
                )
            )
            await session.flush()
            await original_commit(session, fence)

        monkeypatch.setattr(strm_cleanup_plan_module, "_commit_fenced", racing_commit)
        with pytest.raises(StrmCleanupPlanError, match="cleanup_plan_blocked"):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-newer-final",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        assert target.read_bytes() == original_content
        assert (await plan_service.get_plan(plan.plan_id)).status == "needs_review"
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_cleanup_commit_cancel_after_durable_commit_keeps_success(
    tmp_path: Path, monkeypatch
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        async def commit_then_cancel(session, _fence):
            await session.commit()
            raise asyncio.CancelledError

        monkeypatch.setattr(
            strm_cleanup_plan_module, "_commit_fenced", commit_then_cancel
        )
        result = await plan_service.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-cancel-after-commit",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert result.retired == 1
        assert result.plan.status == "applied"
        assert not (tmp_path / "output/Show/Episode.strm").exists()
    finally:
        await database.engine.dispose()


@pytest.mark.parametrize(
    "commit_error",
    (IntegrityError, OperationalError),
    ids=("integrity", "operational"),
)
async def test_same_path_rewrite_restores_old_file_after_commit_failure(
    tmp_path: Path,
    monkeypatch,
    commit_error,
):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        old_content = target.read_bytes()
        await _add_same_path_changed_scan(database)

        async def fail_commit(_session):
            raise commit_error("forced commit failure", {}, RuntimeError("forced"))

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
        summary = await service.incremental(
            "library-strm",
            source_scan_run_id="scan-strm-same-path",
            output_root=tmp_path / "output",
            playback_url_prefix="https://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.failed == 1
        assert target.read_bytes() == old_content
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is not None
            assert manifest.is_current is True
            assert manifest.status == "verified"
            assert manifest.source_version == 1
            assert manifest.size_bytes == 100
    finally:
        await database.engine.dispose()


async def test_same_path_rewrite_restores_old_file_when_commit_is_cancelled(
    tmp_path: Path,
    monkeypatch,
):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        old_content = target.read_bytes()
        await _add_same_path_changed_scan(database)

        async def cancel_commit(_session):
            raise asyncio.CancelledError

        monkeypatch.setattr(AsyncSession, "commit", cancel_commit)
        with pytest.raises(asyncio.CancelledError):
            await service.incremental(
                "library-strm",
                source_scan_run_id="scan-strm-same-path",
                output_root=tmp_path / "output",
                playback_url_prefix="https://127.0.0.1:8115/api/v1/strm/play",
            )

        assert target.read_bytes() == old_content
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            assert manifest is not None
            assert manifest.source_version == 1
            assert manifest.size_bytes == 100
            assert manifest.status == "verified"
    finally:
        await database.engine.dispose()


@pytest.mark.parametrize(
    "commit_error",
    (IntegrityError, OperationalError),
    ids=("integrity", "operational"),
)
async def test_cleanup_commit_failure_restores_file_and_keeps_plan_reviewable(
    tmp_path: Path,
    monkeypatch,
    commit_error,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        original = target.read_bytes()
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        async def fail_commit(_session):
            raise commit_error("forced commit failure", {}, RuntimeError("forced"))

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(StrmCleanupPlanError, match="uncertain"):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-commit-failure",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        assert target.read_bytes() == original
        current_plan = await plan_service.get_plan(plan.plan_id)
        assert current_plan.status == "needs_review"
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_cleanup_restores_file_when_commit_is_cancelled(
    tmp_path: Path,
    monkeypatch,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        original = target.read_bytes()
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        async def cancel_commit(_session):
            raise asyncio.CancelledError

        monkeypatch.setattr(AsyncSession, "commit", cancel_commit)
        with pytest.raises(asyncio.CancelledError):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-cancelled-commit",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        assert target.read_bytes() == original
        assert (await plan_service.get_plan(plan.plan_id)).status == "needs_review"
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_database_lease_takeover_is_fenced_at_manifest_commit(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    peer_database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm.db'}")
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        original = target.read_bytes()
        async with database.session_factory() as session:
            session.add(
                StrmOperation(
                    id="strm_op_fence",
                    library_id="library-strm",
                    kind=StrmOperationKind.INCREMENTAL,
                    source_scan_run_id="scan-strm-same-path",
                    status=StrmOperationStatus.RUNNING,
                    lease_owner="owner-a",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.commit()
        await _add_same_path_changed_scan(database)
        post_write_checks = 0
        taken_over = False

        async def lease_check() -> bool:
            nonlocal post_write_checks, taken_over
            if target.read_bytes() != original:
                post_write_checks += 1
                if post_write_checks == 1 and not taken_over:
                    async with peer_database.session_factory() as session:
                        operation = await session.get(StrmOperation, "strm_op_fence")
                        assert operation is not None
                        operation.lease_owner = "owner-b"
                        operation.lease_expires_at = datetime.now(UTC) + timedelta(
                            minutes=5
                        )
                        await session.commit()
                    taken_over = True
            return True

        with pytest.raises(StrmManifestError, match="strm_operation_lease_lost"):
            await service.incremental(
                "library-strm",
                source_scan_run_id="scan-strm-same-path",
                output_root=tmp_path / "output",
                playback_url_prefix="https://127.0.0.1:8115/api/v1/strm/play",
                lease_check=lease_check,
                operation_id="strm_op_fence",
            )

        assert taken_over is True
        assert target.read_bytes() == original
        async with database.session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100"
                )
            )
            operation = await session.get(StrmOperation, "strm_op_fence")
            assert manifest is not None
            assert manifest.source_version == 1
            assert manifest.size_bytes == 100
            assert operation is not None
            assert operation.lease_owner == "owner-b"
    finally:
        await peer_database.engine.dispose()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_manifest_rejects_operation_from_another_library_scope(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        async with database.session_factory() as session:
            session.add(
                StrmOperation(
                    id="strm_op_wrong_scope",
                    library_id="library-other",
                    kind=StrmOperationKind.FULL,
                    source_scan_run_id="scan-strm",
                    status=StrmOperationStatus.RUNNING,
                    lease_owner="owner-scope",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.commit()

        async def lease_check():
            return True

        with pytest.raises(StrmManifestError, match="strm_operation_lease_lost"):
            await StrmManifestService(database.session_factory).generate(
                "library-strm",
                source_scan_run_id="scan-strm",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                lease_check=lease_check,
                operation_id="strm_op_wrong_scope",
            )
        assert not (tmp_path / "output/Show/Episode.strm").exists()
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_restores_file_when_lease_is_lost_mid_apply(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_running_strm_operation(
            database,
            operation_id="strm_op_cleanup_owner",
            source_scan_run_id="scan-strm-2",
        )
        checks = 0

        async def lease_check() -> bool:
            nonlocal checks
            checks += 1
            return checks < 6

        with pytest.raises(StrmCleanupPlanError, match="strm_operation_lease_lost"):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-lease-loss",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                lease_check=lease_check,
                operation_id="strm_op_cleanup_owner",
            )

        assert (tmp_path / "output/Show/Episode.strm").exists()
        current = await plan_service.get_plan(plan.plan_id)
        assert current.status == "needs_review"
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_manifest_cleanup_does_not_restore_previously_missing_file_on_lease_loss(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        (tmp_path / "output/Show/Episode.strm").unlink()
        await _add_removed_episode_scan(database)
        await _add_running_strm_operation(
            database,
            operation_id="strm_op_cleanup_lost_lease",
            source_scan_run_id="scan-strm-2",
        )
        checks = 0

        async def lease_check() -> bool:
            nonlocal checks
            checks += 1
            return checks < 6

        with pytest.raises(StrmManifestError, match="strm_operation_lease_lost"):
            await manifest_service.cleanup(
                "library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
                lease_check=lease_check,
                operation_id="strm_op_cleanup_lost_lease",
            )

        assert not (tmp_path / "output/Show/Episode.strm").exists()
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_apply_rejects_modified_candidate_without_partial_retirement(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        (tmp_path / "output/Show/Episode.strm").write_text("changed\n")

        with pytest.raises(Exception) as error:
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-apply-two",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
        assert str(error.value) == "cleanup_plan_blocked"
        assert (tmp_path / "output/Show/Episode.strm").read_text() == "changed\n"
    finally:
        await database.engine.dispose()


async def test_cleanup_plan_apply_rejects_candidate_blocked_at_plan_time_after_restore(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        target = tmp_path / "output/Show/Episode.strm"
        expected = target.read_text()
        target.write_text("user content\n")
        await _add_removed_episode_scan(database)
        plan_service = StrmCleanupPlanService(database.session_factory)
        plan = await plan_service.create_plan(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        assert plan.executable_count == 0
        assert plan.blocked_count == 1

        target.write_text(expected)
        with pytest.raises(StrmCleanupPlanError, match="cleanup_plan_blocked"):
            await plan_service.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-restored-blocked",
                output_root=tmp_path / "output",
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        assert target.read_text() == expected
        items, total = await manifest_service.list_current("library-strm")
        assert total == 1
        assert items[0].status == "verified"
    finally:
        await database.engine.dispose()


async def test_strm_verify_reports_valid_and_invalid_managed_content(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        service = StrmVerificationService(database.session_factory)
        valid = await service.verify(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        (tmp_path / "output/Show/Episode.strm").write_text("changed\n")
        invalid = await service.verify(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert valid.status == "verified"
        assert valid.checked_count == 1
        assert valid.valid_count == 1
        assert invalid.status == "issues"
        assert invalid.invalid_count == 1
        assert invalid.issues[0].kind == "invalid_content"
    finally:
        await database.engine.dispose()


async def test_strm_verify_reports_missing_and_orphan_manifest(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmVerificationService(database.session_factory)
        missing = await service.verify(
            library_id="library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path,
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        orphan = await service.verify(
            library_id="library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert missing.missing_count == 1
        assert missing.status == "issues"
        assert orphan.orphan_count == 1
        assert orphan.issues[0].kind == "orphan_manifest"
    finally:
        await database.engine.dispose()


async def _add_changed_scan(database) -> None:
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-strm-2",
                library_id="library-strm",
                root_directory_id="root-strm",
                idempotency_key="scan-key-2",
                state="completed",
                complete=True,
                snapshot_revision=2,
                scan_mode="tree",
                pages_read=1,
                items_seen=2,
                expected_total=2,
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="scan-strm-2",
                    object_type="file",
                    object_id="100",
                    parent_id="root-strm",
                    name="Episode-renamed.mkv",
                    path="Show/Episode-renamed.mkv",
                    is_directory=False,
                    size_bytes=101,
                    pickcode="protected-episode-pickcode-rotated",
                ),
                LibraryScanEntry(
                    scan_run_id="scan-strm-2",
                    object_type="file",
                    object_id="103",
                    parent_id="root-strm",
                    name="New.mkv",
                    path="Show/New.mkv",
                    is_directory=False,
                    size_bytes=300,
                ),
            ]
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-strm-2",
                page=1,
                items_seen=2,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-strm": 2},
                        "expected_total": 2,
                        "pending": [],
                        "visited": ["root-strm"],
                    }
                ),
            )
        )
        session.add_all(
            [
                LibraryScanDiff(
                    scan_run_id="scan-strm-2",
                    object_type="file",
                    object_id="100",
                    change_kind="changed",
                    path_changed=True,
                ),
                LibraryScanDiff(
                    scan_run_id="scan-strm-2",
                    object_type="file",
                    object_id="102",
                    change_kind="removed",
                    path_changed=False,
                ),
                LibraryScanDiff(
                    scan_run_id="scan-strm-2",
                    object_type="file",
                    object_id="103",
                    change_kind="added",
                    path_changed=False,
                ),
            ]
        )
        await session.commit()


async def _add_same_path_changed_scan(database) -> None:
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-strm-same-path",
                library_id="library-strm",
                root_directory_id="root-strm",
                idempotency_key="scan-key-same-path",
                state="completed",
                complete=True,
                snapshot_revision=2,
                scan_mode="tree",
                pages_read=1,
                items_seen=1,
                expected_total=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-strm-same-path",
                object_type="file",
                object_id="100",
                parent_id="root-strm",
                name="Episode.mkv",
                path="Show/Episode.mkv",
                is_directory=False,
                size_bytes=101,
            )
        )
        session.add(
            LibraryScanDiff(
                scan_run_id="scan-strm-same-path",
                object_type="file",
                object_id="100",
                change_kind="changed",
                path_changed=False,
            )
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-strm-same-path",
                page=1,
                items_seen=1,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-strm": 1},
                        "expected_total": 1,
                        "pending": [],
                        "visited": ["root-strm"],
                    }
                ),
            )
        )
        await session.commit()


async def _add_running_strm_operation(
    database,
    *,
    operation_id: str,
    source_scan_run_id: str,
) -> None:
    async with database.session_factory() as session:
        session.add(
            StrmOperation(
                id=operation_id,
                library_id="library-strm",
                kind=StrmOperationKind.CLEANUP,
                source_scan_run_id=source_scan_run_id,
                status=StrmOperationStatus.RUNNING,
                lease_owner="owner-a",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session.commit()


async def _add_removed_episode_scan(database) -> None:
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-strm-2",
                library_id="library-strm",
                root_directory_id="root-strm",
                idempotency_key="scan-key-2",
                state="completed",
                complete=True,
                snapshot_revision=2,
                scan_mode="tree",
                pages_read=1,
                items_seen=1,
                expected_total=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-strm-2",
                object_type="file",
                object_id="101",
                parent_id="root-strm",
                name="notes.txt",
                path="notes.txt",
                is_directory=False,
                size_bytes=10,
            )
        )
        session.add(
            LibraryScanDiff(
                scan_run_id="scan-strm-2",
                object_type="file",
                object_id="100",
                change_kind="removed",
                path_changed=False,
            )
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-strm-2",
                page=1,
                items_seen=1,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-strm": 1},
                        "expected_total": 1,
                        "pending": [],
                        "visited": ["root-strm"],
                    }
                ),
            )
        )
        await session.commit()


async def test_incremental_reconciles_only_complete_scan_diffs(tmp_path: Path):
    database = await _database(tmp_path, include_second_video=True)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_changed_scan(database)
        summary = await service.incremental(
            "library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.generated == 2
        assert summary.retired == 1
        assert summary.failed == 0
        assert not (tmp_path / "output/Show/Episode.strm").exists()
        assert (tmp_path / "output/Show/Episode-renamed.strm").exists()
        assert (tmp_path / "output/Show/New.strm").exists()
        async with database.session_factory() as session:
            retired = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "102"
                )
            )
            updated = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.cloud_file_id == "100",
                    StrmManifestEntry.is_current.is_(True),
                )
            )
            assert retired is not None
            assert retired.is_current is False
            assert retired.status == "retired"
            assert updated is not None
            assert updated.pickcode == "protected-episode-pickcode-rotated"
    finally:
        await database.engine.dispose()


async def test_cleanup_does_not_delete_user_modified_strm(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        service = StrmManifestService(database.session_factory)
        await service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        managed_path = tmp_path / "output/Show/Episode.strm"
        managed_path.write_text("user-edited\n", encoding="utf-8")
        await _add_removed_episode_scan(database)
        summary = await service.cleanup(
            "library-strm",
            source_scan_run_id="scan-strm-2",
            output_root=tmp_path / "output",
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )

        assert summary.failed == 1
        assert summary.retired == 0
        assert managed_path.read_text(encoding="utf-8") == "user-edited\n"
        items, total = await service.list_current("library-strm")
        assert total == 1
        assert items[0].cloud_file_id == "100"
    finally:
        await database.engine.dispose()


async def test_cleanup_and_verification_are_bound_to_managed_output_root(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    allowed = tmp_path / "allowed-output"
    outside = tmp_path / "outside-output"
    outside.mkdir()
    try:
        manifest_service = StrmManifestService(database.session_factory)
        await manifest_service.generate(
            "library-strm",
            source_scan_run_id="scan-strm",
            output_root=allowed,
            playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
        )
        await _add_removed_episode_scan(database)
        cleanup = StrmCleanupPlanService(
            database.session_factory,
            managed_output_roots=(allowed,),
        )
        with pytest.raises(StrmCleanupPlanError, match="strm_output_unavailable"):
            await cleanup.create_plan(
                library_id="library-strm",
                source_scan_run_id="scan-strm-2",
                output_root=outside,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )

        verification = StrmVerificationService(
            database.session_factory,
            managed_output_roots=(allowed,),
        )
        with pytest.raises(StrmVerificationError, match="strm_output_unavailable"):
            await verification.verify(
                library_id="library-strm",
                source_scan_run_id="scan-strm",
                output_root=outside,
                playback_url_prefix="http://127.0.0.1:8115/api/v1/strm/play",
            )
    finally:
        await database.engine.dispose()
