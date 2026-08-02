from pathlib import Path

import pytest
from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    StrmManifestEntry,
)
from watch_assistant.services.strm_cleanup_plan import StrmCleanupPlanService
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    StrmManifestService,
)
from watch_assistant.services.strm_verification import StrmVerificationService


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
        session.add(
            LibraryScanRun(
                id="scan-strm",
                library_id="library-strm",
                root_directory_id="root-strm",
                idempotency_key="scan-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
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
        assert "pickcode" not in content.lower()
        assert "token" not in content.lower()
        assert "cookie" not in content.lower()
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
            assert retired is not None
            assert retired.is_current is False
            assert retired.status == "retired"
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
