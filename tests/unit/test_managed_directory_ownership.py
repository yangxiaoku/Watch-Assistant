from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    ManagedDirectoryOwnershipStatus,
    MediaLibrary,
)
from watch_assistant.services.managed_directory_ownership import (
    ManagedDirectoryOwnershipError,
    ManagedDirectoryOwnershipService,
)


async def _service(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-1",
                name="受管媒体库",
                root_directory_id="100",
                enabled=True,
                scope_verified=True,
                revision=1,
                created_at=now,
            )
        )
        await session.commit()
    return database, ManagedDirectoryOwnershipService(database.session_factory)


@pytest.mark.asyncio
async def test_ownership_survives_service_rebuild_and_transitions_idempotently(
    tmp_path: Path,
):
    database, service = await _service(tmp_path)
    try:
        created = await service.register_created(
            directory_id="300",
            library_id="library-1",
            parent_directory_id="200",
            name="空目录",
            relative_path="受管来源/空目录",
            operation_id="op-directory-1",
        )
        rebuilt = ManagedDirectoryOwnershipService(database.session_factory)
        assert (await rebuilt.list_active("library-1"))["300"] == created

        recycled = await rebuilt.mark_recycled(
            directory_id="300",
            library_id="library-1",
            parent_directory_id="200",
            name="空目录",
            relative_path="受管来源/空目录",
        )
        assert recycled.status == ManagedDirectoryOwnershipStatus.RECYCLED.value
        assert (
            await rebuilt.mark_recycled(
                directory_id="300",
                library_id="library-1",
                parent_directory_id="200",
                name="空目录",
                relative_path="受管来源/空目录",
            )
        ) == recycled

        restored = await rebuilt.mark_restored(
            directory_id="300",
            library_id="library-1",
            parent_directory_id="200",
            name="空目录",
            relative_path="受管来源/空目录",
        )
        assert restored.status == ManagedDirectoryOwnershipStatus.ACTIVE.value
        assert (
            await rebuilt.mark_restored(
                directory_id="300",
                library_id="library-1",
                parent_directory_id="200",
                name="空目录",
                relative_path="受管来源/空目录",
            )
        ) == restored
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_ownership_missing_or_scope_mismatch_fails_closed(tmp_path: Path):
    database, service = await _service(tmp_path)
    try:
        with pytest.raises(
            ManagedDirectoryOwnershipError, match="directory_ownership_missing"
        ):
            await service.mark_recycled(
                directory_id="300",
                library_id="library-1",
                parent_directory_id="200",
                name="空目录",
                relative_path="受管来源/空目录",
            )

        await service.register_created(
            directory_id="300",
            library_id="library-1",
            parent_directory_id="200",
            name="空目录",
            relative_path="受管来源/空目录",
            operation_id="op-directory-1",
        )
        assert (
            await service.get_active(
                "300",
                library_id="library-2",
                parent_directory_id="200",
                name="空目录",
                relative_path="受管来源/空目录",
            )
            is None
        )
        with pytest.raises(
            ManagedDirectoryOwnershipError,
            match="directory_ownership_scope_conflict",
        ):
            await service.mark_recycled(
                directory_id="300",
                library_id="library-1",
                parent_directory_id="201",
                name="空目录",
                relative_path="受管来源/空目录",
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_migration_creates_ownership_table(tmp_path: Path):
    database, _service_instance = await _service(tmp_path)
    try:
        async with database.engine.connect() as connection:
            exists = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(
                    "managed_directory_ownership"
                )
            )
        assert exists
    finally:
        await database.engine.dispose()
