from pathlib import Path

import pytest

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary
from watch_assistant.services.library_index import LibraryIndexService
from watch_assistant.services.strm_cleanup_plan import (
    StrmCleanupPlanError,
    StrmCleanupPlanService,
)
from watch_assistant.services.strm_manifest import StrmManifestService


class _FakeTreeGateway:
    def __init__(self) -> None:
        self.mode = "initial"

    async def list_directory(self, directory_id: str, *, page: int, page_size: int):
        assert page == 1
        assert page_size == 1
        if self.mode == "incomplete" and directory_id == "2000":
            return DirectoryPage(
                items=(),
                page=1,
                page_count=1,
                total=0,
                scan_complete=False,
                state=ScanState.PARTIAL,
                has_more=False,
                terminal=True,
            )
        directory = LibraryEntry(
            directory_id="2000",
            file_id=None,
            parent_id="1000",
            name="Shows",
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
            path="Shows",
        )
        if directory_id == "1000":
            return DirectoryPage(
                items=(directory,),
                page=1,
                page_count=1,
                total=1,
                scan_complete=True,
                state=ScanState.COMPLETE,
                has_more=False,
                terminal=True,
            )
        file_name = {
            "initial": "Episode.mkv",
            "renamed": "Renamed.mkv",
            "removed": None,
        }[self.mode]
        items = () if file_name is None else (
            LibraryEntry(
                directory_id=None,
                file_id="3000",
                parent_id="2000",
                name=file_name,
                is_directory=False,
                size_bytes=10,
                modified_at=None,
                pickcode=None,
                path=None,
            ),
        )
        return DirectoryPage(
            items=items,
            page=1,
            page_count=1,
            total=len(items),
            scan_complete=True,
            state=ScanState.COMPLETE,
            has_more=False,
            terminal=True,
        )


@pytest.mark.asyncio
async def test_fake_tree_full_incremental_and_reviewed_cleanup(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm-contract.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="本地测试媒体库",
                root_directory_id="1000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()

    gateway = _FakeTreeGateway()
    index = LibraryIndexService(
        database.session_factory,
        gateway,
        library_id="library-one",
        root_directory_id="1000",
        page_size=1,
    )
    manifest = StrmManifestService(database.session_factory)
    cleanup = StrmCleanupPlanService(database.session_factory)
    output = tmp_path / "strm-output"
    prefix = "http://127.0.0.1:8115/api/v1/strm/play"
    try:
        first_scan = await index.scan_tree("scan-initial")
        assert first_scan.complete is True
        first = await manifest.generate(
            "library-one",
            source_scan_run_id=first_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert first.generated == 1
        first_items, _ = await manifest.list_current("library-one")
        first_manifest_id = first_items[0].manifest_id
        assert (output / "Shows/Episode.strm").exists()

        gateway.mode = "renamed"
        renamed_scan = await index.scan_tree("scan-renamed")
        renamed = await manifest.incremental(
            "library-one",
            source_scan_run_id=renamed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
            retire_removed=False,
        )
        assert renamed.generated == 1
        renamed_items, _ = await manifest.list_current("library-one")
        assert renamed_items[0].manifest_id == first_manifest_id
        assert not (output / "Shows/Episode.strm").exists()
        assert (output / "Shows/Renamed.strm").exists()

        gateway.mode = "removed"
        removed_scan = await index.scan_tree("scan-removed")
        pending = await manifest.incremental(
            "library-one",
            source_scan_run_id=removed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
            retire_removed=False,
        )
        assert pending.retired == 0
        assert (output / "Shows/Renamed.strm").exists()
        (output / "Shows/user-owned.txt").write_text("keep\n", encoding="utf-8")

        plan = await cleanup.create_plan(
            library_id="library-one",
            source_scan_run_id=removed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert plan.candidate_count == 1
        applied = await cleanup.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-contract-one",
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert applied.retired == 1
        assert not (output / "Shows/Renamed.strm").exists()
        assert (output / "Shows/user-owned.txt").exists()
        repeated = await cleanup.apply_plan(
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            digest=plan.plan_hash,
            confirm=True,
            idempotency_key="cleanup-contract-one",
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert repeated.retired == 1
        with pytest.raises(StrmCleanupPlanError, match="cleanup_plan_already_applied"):
            await cleanup.apply_plan(
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                digest=plan.plan_hash,
                confirm=True,
                idempotency_key="cleanup-contract-two",
                output_root=output,
                playback_url_prefix=prefix,
            )

        gateway.mode = "incomplete"
        incomplete_scan = await index.scan_tree("scan-incomplete")
        assert incomplete_scan.complete is False
        with pytest.raises(StrmCleanupPlanError, match="source_snapshot_not_ready"):
            await cleanup.create_plan(
                library_id="library-one",
                source_scan_run_id=incomplete_scan.run_id,
                output_root=output,
                playback_url_prefix=prefix,
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_cleanup_apply_does_not_orphan_strm(tmp_path: Path):
    """两个并发 apply_plan(绕过 ledger)必须互斥:后提交者不得把已 retire
    manifest 的 .strm 文件回滚写回磁盘,产生孤儿文件。"""
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'sync-race.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="ONE",
                root_directory_id="1000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()

    gateway = _FakeTreeGateway()
    index = LibraryIndexService(
        database.session_factory,
        gateway,
        library_id="library-one",
        root_directory_id="1000",
        page_size=1,
    )
    manifest = StrmManifestService(database.session_factory)
    cleanup = StrmCleanupPlanService(database.session_factory)
    output = tmp_path / "strm-race-output"
    prefix = "http://127.0.0.1:8115/api/v1/strm/play"
    try:
        first_scan = await index.scan_tree("scan-race-initial")
        await manifest.generate(
            "library-one",
            source_scan_run_id=first_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert (output / "Shows/Episode.strm").exists()

        gateway.mode = "renamed"
        renamed_scan = await index.scan_tree("scan-race-renamed")
        await manifest.incremental(
            "library-one",
            source_scan_run_id=renamed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
            retire_removed=False,
        )

        gateway.mode = "removed"
        removed_scan = await index.scan_tree("scan-race-removed")
        await manifest.incremental(
            "library-one",
            source_scan_run_id=removed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
            retire_removed=False,
        )

        plan = await cleanup.create_plan(
            library_id="library-one",
            source_scan_run_id=removed_scan.run_id,
            output_root=output,
            playback_url_prefix=prefix,
        )
        assert plan.candidate_count == 1

        import asyncio as _asyncio

        async def apply_one():
            try:
                return await cleanup.apply_plan(
                    plan_id=plan.plan_id,
                    expected_revision=plan.revision,
                    digest=plan.plan_hash,
                    confirm=True,
                    idempotency_key="race-one",
                    output_root=output,
                    playback_url_prefix=prefix,
                )
            except StrmCleanupPlanError:
                return None

        async def apply_two():
            try:
                return await cleanup.apply_plan(
                    plan_id=plan.plan_id,
                    expected_revision=plan.revision,
                    digest=plan.plan_hash,
                    confirm=True,
                    idempotency_key="race-two",
                    output_root=output,
                    playback_url_prefix=prefix,
                )
            except StrmCleanupPlanError:
                return None

        # 真实并发:两个 apply 同时发起(绕过 ledger)。apply_plan 无进程内锁,
        # 依赖数据库占用(applying 中间态)保证互斥。核心安全属性:并发下
        # 不得产生孤儿 .strm 文件(后提交者不得把已 retire 的文件回滚写回)。
        results = await _asyncio.gather(apply_one(), apply_two())
        successful = [r for r in results if r is not None]
        assert successful, "至少一个 apply 应成功"
        assert all(r.retired == 1 for r in successful)

        # 核心断言:磁盘上不得有孤儿 .strm 文件
        strm_files = list(output.rglob("*.strm"))
        assert not strm_files, f"发现孤儿 .strm 文件: {strm_files}"

        # 最终清单里该 manifest 必须已 retire
        final_items, _ = await manifest.list_current("library-one")
        assert len(final_items) == 0
    finally:
        await database.engine.dispose()
