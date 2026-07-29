from pathlib import Path

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.strm_manifest import StrmManifestService


async def _database(tmp_path: Path):
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
        session.add_all(
            [
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
        )
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
        assert content == f"http://127.0.0.1:8115/api/v1/strm/play/{items[0].manifest_id}\n"
    finally:
        await database.engine.dispose()
