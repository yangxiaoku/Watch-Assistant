import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from watch_assistant.adapters.p115_library import (
    DirectoryPage,
    LibraryEntry,
    ScanState,
)
from watch_assistant.app import _refresh_inventory_before_push
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    DirectoryFingerprint,
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
        self.fingerprint = (2, "1700000000", 1)

    async def list_directory(self, directory_id: str, *, page: int, page_size: int):
        assert page_size == 50
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

    async def get_file_detail(self, file_id: str) -> LibraryEntry:
        parent_id = ROOT_ID if file_id == "1000" else NESTED_ID
        return LibraryEntry(
            directory_id=None,
            file_id=file_id,
            parent_id=parent_id,
            name="Inventory fixture.mkv",
            is_directory=False,
            size_bytes=100,
            modified_at=None,
            pickcode="inventory-fixture-pickcode",
            path=None,
        )

    async def get_directory_fingerprint(
        self, directory_id: str
    ) -> tuple[int, str | None, int | None] | None:
        # 本地缓存 P1:指纹核对入口,1 次 fs_info 语义。
        return self.fingerprint


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
        page_size=50,
    ).scan_tree("initial-tree")

    application = FastAPI()
    application.state.database = database
    application.state.organization_cookie_provider = object()
    application.state.settings_service = None
    gateway = _RecursiveGateway()
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway",
        lambda *_a, **_k: gateway,
    )

    # 指纹过期(超过 TTL)→ L1 核对:指纹未变,仅更新核对时间,不触发全树。
    async with database.session_factory() as session:
        fingerprint = await session.get(
            DirectoryFingerprint, (LIBRARY_ID, ROOT_ID)
        )
        assert fingerprint is not None
        fingerprint.verified_at = datetime.now(UTC) - timedelta(minutes=61)
        await session.commit()

    refreshed = await _refresh_inventory_before_push(application)

    assert refreshed.complete is True
    assert refreshed.scope_verified is True
    assert refreshed.refreshed_count == 0  # L1 核对通过,未升级全树扫描
    assert gateway.calls == []  # 未访问任何目录列表页

    async with database.session_factory() as session:
        fingerprint = await session.get(
            DirectoryFingerprint, (LIBRARY_ID, ROOT_ID)
        )
        assert fingerprint is not None
        verified_at = fingerprint.verified_at
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=UTC)
        assert (datetime.now(UTC) - verified_at).total_seconds() < 60  # 已刷新

    duplicate = await InventoryPushGuard(database.session_factory).check(
        "resource-refresh"
    )
    assert duplicate.allowed is False
    assert duplicate.code == "inventory_exact_duplicate"
    await database.engine.dispose()


@pytest.mark.integration
async def test_push_refresh_upgrades_to_full_scan_when_fingerprint_changes(
    tmp_path, monkeypatch
):
    """指纹变化(远端 count 不同)→ L1 核对失败 → 升级全树扫描并重写指纹。"""
    database = await _database(tmp_path)
    await LibraryIndexService(
        database.session_factory,
        _RecursiveGateway(),
        library_id=LIBRARY_ID,
        root_directory_id=ROOT_ID,
        page_size=50,
    ).scan_tree("initial-tree")

    application = FastAPI()
    application.state.database = database
    application.state.organization_cookie_provider = object()
    application.state.settings_service = None
    gateway = _RecursiveGateway()
    gateway.fingerprint = (3, "1700000000", 1)  # 远端 count 变化
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway",
        lambda *_a, **_k: gateway,
    )
    async with database.session_factory() as session:
        fingerprint = await session.get(
            DirectoryFingerprint, (LIBRARY_ID, ROOT_ID)
        )
        fingerprint.verified_at = datetime.now(UTC) - timedelta(minutes=61)
        await session.commit()

    refreshed = await _refresh_inventory_before_push(application)

    assert refreshed.complete is True
    assert refreshed.refreshed_count == 1
    assert gateway.calls  # 升级了全树扫描
    async with database.session_factory() as session:
        fingerprint = await session.get(
            DirectoryFingerprint, (LIBRARY_ID, ROOT_ID)
        )
        assert fingerprint.child_count == 3  # 指纹已重写
    await database.engine.dispose()
