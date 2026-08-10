from pathlib import Path

import pytest

from watch_assistant.app import _current_managed_directory_ids
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.strm_scope import (
    normalize_playback_url_prefix,
    source_snapshot_is_current,
)


@pytest.mark.asyncio
async def test_cleanup_scope_uses_one_library_snapshot(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add_all(
                [
                    MediaLibrary(
                        id="library-a",
                        name="媒体库 A",
                        root_directory_id="1000",
                        scope_verified=True,
                        enabled=True,
                    ),
                    MediaLibrary(
                        id="library-b",
                        name="媒体库 B",
                        root_directory_id="2000",
                        scope_verified=True,
                        enabled=True,
                    ),
                ]
            )
            await session.commit()
            session.add_all(
                [
                    LibraryScanRun(
                        id="scan-a",
                        library_id="library-a",
                        root_directory_id="1000",
                        idempotency_key="scan-a-key",
                        state="completed",
                        complete=True,
                        snapshot_revision=1,
                    ),
                    LibraryScanRun(
                        id="scan-b",
                        library_id="library-b",
                        root_directory_id="2000",
                        idempotency_key="scan-b-key",
                        state="completed",
                        complete=True,
                        snapshot_revision=1,
                    ),
                ]
            )
            await session.commit()
            session.add_all(
                [
                    LibraryScanEntry(
                        scan_run_id="scan-a",
                        object_type="directory",
                        object_id="1100",
                        parent_id="1000",
                        name="A",
                        path="A",
                        is_directory=True,
                    ),
                    LibraryScanEntry(
                        scan_run_id="scan-b",
                        object_type="directory",
                        object_id="2100",
                        parent_id="2000",
                        name="B",
                        path="B",
                        is_directory=True,
                    ),
                ]
            )
            await session.commit()

        scope = await _current_managed_directory_ids(
            database.session_factory, "1100", "1000"
        )
        assert scope == frozenset({"1000", "1100"})
        assert await _current_managed_directory_ids(
            database.session_factory, "1100", "2000"
        ) == frozenset()
    finally:
        await database.engine.dispose()


def test_playback_prefix_is_the_stable_route_without_sensitive_url_parts():
    assert (
        normalize_playback_url_prefix(
            "https://watch-assistant.example/api/v1/strm/play/"
        )
        == "https://watch-assistant.example/api/v1/strm/play/"
    )
    for invalid in (
        "https://watch-assistant.example/api/v1/strm/play?x=1",
        "https://watch-assistant.example/api/v1/strm/play/other",
        "https://user:credential@watch-assistant.example/api/v1/strm/play",
        "https://watch-assistant.example/api/v1/strm/play#fragment",
        "https://watch-assistant.example/strm/play",
    ):
        with pytest.raises(ValueError, match="invalid_playback_url_prefix"):
            normalize_playback_url_prefix(invalid)


@pytest.mark.asyncio
async def test_duplicate_complete_snapshot_revision_fails_closed(tmp_path: Path):
    """重复 revision 在写入层被唯一索引拒绝(迁移 072),歧义快照不可达。"""
    from sqlalchemy.exc import IntegrityError

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'ambiguous.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                MediaLibrary(
                    id="library-ambiguous",
                    name="媒体库",
                    root_directory_id="1000",
                    scope_verified=True,
                    enabled=True,
                )
            )
            await session.flush()
            session.add(
                LibraryScanRun(
                    id="scan-a",
                    library_id="library-ambiguous",
                    root_directory_id="1000",
                    idempotency_key="scan-a-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=1,
                )
            )
            await session.flush()
            session.add(
                LibraryScanRun(
                    id="scan-b",
                    library_id="library-ambiguous",
                    root_directory_id="1000",
                    idempotency_key="scan-b-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        await database.engine.dispose()
