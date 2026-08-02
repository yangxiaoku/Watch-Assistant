import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from watch_assistant.adapters.p115_library import (
    DirectoryPage,
    LibraryEntry,
    ScanState,
)
from watch_assistant.app import _refresh_inventory_before_push
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import Resource
from watch_assistant.services.inventory_push_guard import InventoryPushGuard
from watch_assistant.services.library_index import LibraryIndexService

ROOT_ID = "7000"
NESTED_ID = "7100"
LIBRARY_ID = "library-refresh"
NESTED_FILE_ID = "1001"


class _RecursiveGateway:
    def __init__(self, *_args, **_kwargs):
        self.calls: list[tuple[str, int]] = []

    async def list_directory(self, directory_id: str, *, page: int, page_size: int):
        assert page_size == 1
        self.calls.append((directory_id, page))
        if directory_id == ROOT_ID and page == 1:
            return _page(
                page,
                (
                    LibraryEntry(
                        directory_id=NESTED_ID,
                        file_id=None,
                        parent_id=ROOT_ID,
                        name="Nested",
                        is_directory=True,
                        size_bytes=None,
                        modified_at=None,
                        pickcode=None,
                        path="Nested",
                    ),
                ),
                page_count=2,
                total=2,
                terminal=False,
                has_more=True,
            )
        if directory_id == ROOT_ID and page == 2:
            return _page(
                page,
                (
                    LibraryEntry(
                        directory_id=None,
                        file_id="1000",
                        parent_id=ROOT_ID,
                        name="Root.Movie.2026.mkv",
                        is_directory=False,
                        size_bytes=100,
                        modified_at=None,
                        pickcode=None,
                        path="Root.Movie.2026.mkv",
                    ),
                ),
                page_count=2,
                total=2,
                terminal=True,
                has_more=False,
            )
        if directory_id == NESTED_ID and page == 1:
            return _page(
                page,
                (
                    LibraryEntry(
                        directory_id=None,
                        file_id=NESTED_FILE_ID,
                        parent_id=NESTED_ID,
                        name="Nested.Movie.2026.mkv",
                        is_directory=False,
                        size_bytes=200,
                        modified_at=None,
                        pickcode=None,
                        path="Nested.Movie.2026.mkv",
                    ),
                ),
                page_count=1,
                total=1,
                terminal=True,
                has_more=False,
            )
        raise AssertionError(f"unexpected directory page: {directory_id}:{page}")


def _page(page, items, *, page_count, total, terminal, has_more):
    return DirectoryPage(
        items=tuple(items),
        page=page,
        page_count=page_count,
        total=total,
        scan_complete=True if terminal else None,
        state=ScanState.COMPLETE,
        terminal=terminal,
        has_more=has_more,
    )


async def _database(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'inventory-refresh.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="测试媒体库",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        session.add(
            Resource(
                id="resource-refresh",
                kind="magnet",
                canonical_key="refresh-test-resource",
                encrypted_url="encrypted-test-value",
                name="Nested.Movie.2026.mkv",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                metadata_json=json.dumps({"object_id": NESTED_FILE_ID}),
            )
        )
        await session.commit()
    return database


@pytest.mark.integration
async def test_push_refresh_preserves_nested_inventory_and_blocks_exact_duplicate(
    tmp_path, monkeypatch
):
    database = await _database(tmp_path)
    await LibraryIndexService(
        database.session_factory,
        _RecursiveGateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=1,
    ).scan_tree("initial-tree")

    application = FastAPI()
    application.state.database = database
    application.state.organization_cookie_provider = object()
    application.state.settings_service = None
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway", _RecursiveGateway
    )

    refreshed = await _refresh_inventory_before_push(application)

    assert refreshed.complete is True
    assert refreshed.scope_verified is True
    async with database.session_factory() as session:
        latest = await session.scalar(
            select(LibraryScanRun)
            .where(LibraryScanRun.library_id == LIBRARY_ID)
            .order_by(LibraryScanRun.snapshot_revision.desc())
            .limit(1)
        )
        assert latest is not None
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == latest.id
                    )
                )
            ).all()
        )
    assert {entry.object_id for entry in entries} >= {NESTED_ID, NESTED_FILE_ID}

    duplicate = await InventoryPushGuard(database.session_factory).check(
        "resource-refresh"
    )
    assert duplicate.allowed is False
    assert duplicate.code == "inventory_exact_duplicate"
    await database.engine.dispose()
