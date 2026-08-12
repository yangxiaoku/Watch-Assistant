import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryMediaIdentity,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import Resource
from watch_assistant.services.inventory_push_guard import InventoryPushGuard


async def _database(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    await initialize_database(database.engine)
    return database


async def _resource(database, crypto, *, metadata=None, name="New.Movie.2026.mkv"):
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="resource-guard",
                kind="magnet",
                canonical_key="magnet:abcdef0123456789abcdef0123456789abcdef01",
                encrypted_url=crypto.encrypt("magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"),
                name=name,
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                metadata_json=json.dumps(metadata or {}),
            )
        )
        await session.commit()


async def _library(
    database,
    *,
    complete=True,
    captured_at=None,
    object_id=None,
    name="New.Movie.2026.mkv",
    tmdb_id=None,
    media_type=None,
    scan_mode="tree",
):
    now = captured_at or datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-guard",
                name="测试库",
                root_directory_id="root-guard",
                enabled=True,
                scope_verified=True,
            )
        )
        await session.flush()
        run = LibraryScanRun(
            id="scan-guard",
            library_id="library-guard",
            root_directory_id="root-guard",
            idempotency_key="scan-guard-key",
            scan_mode=scan_mode,
            state="completed" if complete else "failed",
            complete=complete,
            snapshot_revision=1 if complete else None,
            expected_total=1 if object_id else 0,
            pages_read=1,
            items_seen=1 if object_id else 0,
            updated_at=now,
        )
        session.add(run)
        await session.flush()
        if object_id:
            session.add(
                LibraryScanEntry(
                    scan_run_id="scan-guard",
                    object_type="file",
                    object_id=object_id,
                    parent_id="root-guard",
                    name=name,
                    path=name,
                    is_directory=False,
                )
            )
            if tmdb_id is not None:
                session.add(
                    LibraryMediaIdentity(
                        id="identity-guard",
                        library_id="library-guard",
                        object_id=object_id,
                        tmdb_id=tmdb_id,
                        media_type=media_type or "movie",
                    )
                )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-guard",
                page=1,
                items_seen=1 if object_id else 0,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {
                            "root-guard": 1 if object_id else 0
                        },
                        "expected_total": 1 if object_id else 0,
                        "pending": [],
                        "visited": ["root-guard"],
                    }
                ),
            )
        )
        await session.commit()


async def test_missing_scope_blocks_before_remote_submission(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)

    result = await InventoryPushGuard(database.session_factory).check("resource-guard")

    assert result.allowed is False
    assert result.code == "inventory_scope_unconfigured"
    await database.engine.dispose()


async def test_incomplete_scope_blocks_and_fresh_empty_scope_allows(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)
    await _library(database, complete=False)
    guard = InventoryPushGuard(database.session_factory)

    incomplete = await guard.check("resource-guard")
    assert incomplete.code == "inventory_index_incomplete"

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "scan-guard")
        run.complete = True
        run.state = "completed"
        run.snapshot_revision = 2
        run.updated_at = datetime.now(UTC)
        await session.commit()
    allowed = await guard.check("resource-guard")
    assert allowed.allowed is True
    assert allowed.code == "inventory_not_found"
    await database.engine.dispose()


async def test_root_only_scan_cannot_prove_inventory_is_complete(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto, metadata={"object_id": "remote-file"})
    await _library(database, object_id="remote-file", scan_mode="root")

    result = await InventoryPushGuard(database.session_factory).check("resource-guard")

    assert result.allowed is False
    assert result.code == "inventory_index_incomplete"
    await database.engine.dispose()


async def test_ambiguous_complete_snapshot_revision_blocks_inventory_push(tmp_path):
    """迁移 072:重复 snapshot revision 在写入层被唯一索引拒绝,歧义快照不可达。"""
    from sqlalchemy.exc import IntegrityError

    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)
    await _library(database)
    try:
        async with database.session_factory() as session:
            now = datetime.now(UTC)
            session.add(
                LibraryScanRun(
                    id="scan-guard-duplicate",
                library_id="library-guard",
                root_directory_id="root-guard",
                idempotency_key="scan-guard-duplicate-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=0,
                pages_read=1,
                items_seen=0,
                created_at=now + timedelta(seconds=1),
                updated_at=now + timedelta(seconds=1),
            )
        )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

            # 单一 revision 的库不受影响,库存推送正常放行
            result = await InventoryPushGuard(database.session_factory).check(
                "resource-guard"
            )
            assert result.allowed is True
    finally:
        await database.engine.dispose()

async def test_newer_requeued_scan_blocks_inventory_push(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)
    await _library(database)
    async with database.session_factory() as session:
        now = datetime.now(UTC)
        session.add(
            LibraryScanRun(
                id="scan-guard-requeued",
                library_id="library-guard",
                root_directory_id="root-guard",
                idempotency_key="scan-guard-requeued-key",
                scan_mode="tree",
                state="queued",
                complete=False,
                snapshot_revision=None,
                created_at=now - timedelta(minutes=1),
                updated_at=now + timedelta(seconds=1),
            )
        )
        await session.commit()

    try:
        result = await InventoryPushGuard(database.session_factory).check(
            "resource-guard"
        )
        assert result.allowed is False
        assert result.code == "inventory_index_incomplete"
    finally:
        await database.engine.dispose()


async def test_malformed_complete_scope_blocks_inventory_push(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto, metadata={"object_id": "remote-file"})
    await _library(database, object_id="remote-file")
    async with database.session_factory() as session:
        checkpoint = await session.get(LibraryScanCheckpoint, "scan-guard")
        assert checkpoint is not None
        checkpoint.items_seen = 2
        await session.commit()

    result = await InventoryPushGuard(database.session_factory).check("resource-guard")

    assert result.allowed is False
    assert result.code == "inventory_index_incomplete"
    await database.engine.dispose()


async def test_exact_identity_and_stale_scope_are_blocked(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto, metadata={"object_id": "remote-file"})
    await _library(database, object_id="remote-file")
    guard = InventoryPushGuard(database.session_factory)

    duplicate = await guard.check("resource-guard")
    assert duplicate.allowed is False
    assert duplicate.code == "inventory_exact_duplicate"

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "scan-guard")
        run.updated_at = datetime.now(UTC) - timedelta(minutes=16)
        await session.commit()
    stale = await guard.check("resource-guard")
    assert stale.code == "inventory_index_stale"
    await database.engine.dispose()


async def test_media_and_version_duplicates_are_advisory(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    guard = InventoryPushGuard(database.session_factory)

    await _resource(
        database,
        crypto,
        name="New.Movie.2026.2160p.BluRay.mkv",
        metadata={"tmdb_id": 7, "media_type": "movie"},
    )
    await _library(
        database,
        object_id="remote-file",
        name="New.Movie.2026.1080p.WEB-DL.mkv",
        tmdb_id=7,
        media_type="movie",
    )

    media_duplicate = await guard.check("resource-guard")
    assert media_duplicate.allowed is True
    assert media_duplicate.code == "inventory_media_duplicate"

    async with database.session_factory() as session:
        resource = await session.get(Resource, "resource-guard")
        assert resource is not None
        resource.name = "New.Movie.2026.1080p.WEB-DL.mkv"
        await session.commit()

    version_duplicate = await guard.check("resource-guard")
    assert version_duplicate.allowed is True
    assert version_duplicate.code == "inventory_version_duplicate"
    await database.engine.dispose()


async def test_unconfirmed_filename_identity_remains_advisory(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)
    await _library(database, object_id="remote-file")

    result = await InventoryPushGuard(database.session_factory).check("resource-guard")

    assert result.allowed is True
    assert result.code == "inventory_review_required"
    await database.engine.dispose()


async def test_missing_resource_fails_closed(tmp_path):
    """不存在的 resource id 必须 fail-closed(resource_not_found)。"""
    database = await _database(tmp_path)

    result = await InventoryPushGuard(database.session_factory).check(
        "resource-missing"
    )

    assert result.allowed is False
    assert result.code == "resource_not_found"
    await database.engine.dispose()


async def test_non_magnet_resource_is_not_applicable(tmp_path):
    """非 magnet 资源(如 share)不适用库存推送,应放行而非 fail。"""
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="resource-share",
                kind="115_share",
                canonical_key="share:abc",
                encrypted_url=crypto.encrypt("https://115.com/s/abc"),
                name="share-item",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.commit()

    result = await InventoryPushGuard(database.session_factory).check(
        "resource-share"
    )

    assert result.allowed is True
    assert result.code == "not_applicable"
    await database.engine.dispose()


async def test_unverified_scope_blocks_push(tmp_path):
    """库 scope_verified=False 时必须 fail-closed,不得推送。"""
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _resource(database, crypto)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-unverified",
                name="未验证库",
                root_directory_id="root-unverified",
                enabled=True,
                scope_verified=False,
            )
        )
        await session.commit()

    result = await InventoryPushGuard(database.session_factory).check(
        "resource-guard"
    )

    assert result.allowed is False
    assert result.code == "inventory_scope_unconfigured"
    await database.engine.dispose()
