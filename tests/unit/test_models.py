from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from watch_assistant.config import Settings
from watch_assistant.db import cleanup_expired, create_database, initialize_database
from watch_assistant.models import (
    MagnetMetadataCache,
    Resource,
    SearchCache,
    Task,
    TaskState,
    WebSession,
)
from watch_assistant.schemas import InspectionItemStatus, ResourceKind


def test_task_states_are_explicit():
    assert {state.value for state in TaskState} == {
        "queued",
        "submitting",
        "submitted",
        "downloading",
        "available",
        "needs_auth",
        "failed",
        "uncertain",
        "cancelled",
    }


def test_missing_setting_names_the_required_environment_variable(monkeypatch):
    required = {
        "DATABASE_URL": "sqlite+aiosqlite:///test.db",
        "ENCRYPTION_KEY": "test-key",
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TMDB_API_KEY")

    with pytest.raises(ValidationError, match="TMDB_API_KEY"):
        Settings(_env_file=None)


def test_tmdb_base_url_can_use_network_reachable_alias(monkeypatch):
    required = {
        "DATABASE_URL": "sqlite+aiosqlite:///test.db",
        "ENCRYPTION_KEY": "test-key",
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TMDB_BASE_URL": "https://api.tmdb.org/3",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.tmdb_base_url == "https://api.tmdb.org/3"


def test_inspection_settings_load_qb_credentials_from_a_secret_directory(
    tmp_path, monkeypatch
):
    required = {
        "DATABASE_URL": "sqlite+aiosqlite:///test.db",
        "ENCRYPTION_KEY": "test-key",
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "INSPECTION_ENABLED": "true",
        "QBITTORRENT_BASE_URL": "http://172.20.0.4:8080",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "qbittorrent_username").write_text("inspector", encoding="utf-8")
    (tmp_path / "qbittorrent_password").write_text("test-password", encoding="utf-8")

    settings = Settings(_env_file=None, _secrets_dir=tmp_path)

    assert settings.inspection_configured is True


@pytest.mark.asyncio
async def test_initialize_database_adds_web_sessions_without_losing_existing_rows(
    tmp_path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_existing",
                kind=ResourceKind.MAGNET,
                canonical_key="magnet:existing",
                encrypted_url="encrypted",
                name="Existing",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.commit()

    async with database.engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE web_sessions")
    await initialize_database(database.engine)

    async with database.session_factory() as session:
        assert await session.get(Resource, "res_existing") is not None
        assert await session.scalar(select(WebSession)) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_keeps_resource_referenced_by_uncertain_task(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    old_resource_time = now - timedelta(days=31)
    old_task_time = now - timedelta(days=91)

    async with database.session_factory() as session:
        journal_mode = await session.scalar(text("PRAGMA journal_mode"))
        session.add_all(
            [
                Resource(
                    id="res_protected",
                    kind="magnet",
                    canonical_key="magnet:protected",
                    encrypted_url="encrypted-protected",
                    name="Protected",
                    source="PanSou",
                    captured_at=old_resource_time,
                    expires_at=old_resource_time,
                    created_at=old_resource_time,
                ),
                Resource(
                    id="res_orphan",
                    kind="magnet",
                    canonical_key="magnet:orphan",
                    encrypted_url="encrypted-orphan",
                    name="Orphan",
                    source="PanSou",
                    captured_at=old_resource_time,
                    expires_at=old_resource_time,
                    created_at=old_resource_time,
                ),
                Resource(
                    id="res_terminal",
                    kind=ResourceKind.SHARE,
                    canonical_key="share:terminal",
                    encrypted_url="encrypted-terminal",
                    name="Terminal",
                    source="PanSou",
                    captured_at=old_resource_time,
                    expires_at=old_resource_time,
                    created_at=old_resource_time,
                ),
                SearchCache(
                    cache_key="expired",
                    resource_ids_json="[]",
                    warnings_json="[]",
                    fetched_at=old_resource_time,
                    expires_at=old_resource_time,
                ),
                MagnetMetadataCache(
                    infohash="d" * 40,
                    status=InspectionItemStatus.TIMEOUT,
                    schema_version=1,
                    updated_at=old_resource_time,
                    expires_at=old_resource_time,
                ),
                MagnetMetadataCache(
                    infohash="e" * 40,
                    status=InspectionItemStatus.VERIFIED,
                    schema_version=1,
                    updated_at=now,
                    expires_at=None,
                ),
                MagnetMetadataCache(
                    infohash="f" * 40,
                    status=InspectionItemStatus.TIMEOUT,
                    schema_version=1,
                    updated_at=now,
                    expires_at=now + timedelta(minutes=30),
                ),
            ]
        )
        session.add_all(
            [
                Task(
                    id="task_uncertain",
                    resource_id="res_protected",
                    action="offline_download",
                    encrypted_url_snapshot="snapshot",
                    state=TaskState.UNCERTAIN,
                    created_at=now,
                    updated_at=now,
                ),
                Task(
                    id="task_failed",
                    resource_id="res_terminal",
                    action="save_share",
                    encrypted_url_snapshot="snapshot",
                    state=TaskState.FAILED,
                    created_at=old_task_time,
                    updated_at=old_task_time,
                ),
            ]
        )
        await session.commit()

        result = await cleanup_expired(session, now=now)
        await session.commit()

        resource_ids = set(await session.scalars(select(Resource.id)))
        task_ids = set(await session.scalars(select(Task.id)))
        cache_keys = set(await session.scalars(select(SearchCache.cache_key)))
        metadata_cache_infohashes = set(
            await session.scalars(select(MagnetMetadataCache.infohash))
        )

    await database.engine.dispose()

    assert resource_ids == {"res_protected"}
    assert journal_mode == "wal"
    assert task_ids == {"task_uncertain"}
    assert cache_keys == set()
    assert metadata_cache_infohashes == {"e" * 40, "f" * 40}
    assert result.resources_deleted == 2
    assert result.tasks_deleted == 1
    assert result.cache_entries_deleted == 2
