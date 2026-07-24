from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from watch_assistant.config import Settings
from watch_assistant.db import cleanup_expired, create_database, initialize_database
from watch_assistant.models import Resource, SearchCache, Task, TaskState


def test_task_states_are_explicit():
    assert {state.value for state in TaskState} == {
        "queued",
        "submitting",
        "accepted",
        "needs_auth",
        "failed",
        "uncertain",
    }


def test_missing_setting_names_the_required_environment_variable(monkeypatch):
    required = {
        "DATABASE_URL": "sqlite+aiosqlite:///test.db",
        "ENCRYPTION_KEY": "test-key",
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TGTO_BASE_URL": "http://tgto.test",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TMDB_API_KEY")

    with pytest.raises(ValidationError, match="TMDB_API_KEY"):
        Settings(_env_file=None)


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
                    kind="share",
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

    await database.engine.dispose()

    assert resource_ids == {"res_protected"}
    assert journal_mode == "wal"
    assert task_ids == {"task_uncertain"}
    assert cache_keys == set()
    assert result.resources_deleted == 2
    assert result.tasks_deleted == 1
    assert result.cache_entries_deleted == 1
