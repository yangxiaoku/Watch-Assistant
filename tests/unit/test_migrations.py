from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

import watch_assistant.db as database_module
from watch_assistant.db import create_database, initialize_database
from watch_assistant.migrations import MIGRATIONS, Migration, run_migrations
from watch_assistant.models import ApplicationSettings, Resource, Task, WebSession
from watch_assistant.schemas import ResourceKind, TaskAction

APPLICATION_SETTINGS_COLUMNS = {
    "id",
    "logging_level",
    "retention_days",
    "max_file_mb",
    "inspection_auto_start_enabled",
    "revision",
    "content_policy_json",
    "managed_tmdb_key_encrypted",
    "managed_tmdb_updated_at",
    "managed_p115_cookie_encrypted",
    "managed_p115_updated_at",
    "updated_at",
}


async def _applied_migration_ids(database) -> list[str]:
    async with database.engine.connect() as connection:
        return list(
            await connection.scalars(
                text("SELECT migration_id FROM schema_migrations ORDER BY migration_id")
            )
        )


@pytest.mark.asyncio
async def test_initialize_database_creates_schema_records_migration_and_defaults(
    tmp_path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")

    await initialize_database(database.engine)

    async with database.engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns(
                    "application_settings"
                )
            }
        )
        integrity_check = await connection.scalar(text("PRAGMA integrity_check"))
    async with database.session_factory() as session:
        session.add(ApplicationSettings(id="default"))
        await session.commit()
        settings = await session.get(ApplicationSettings, "default")

    assert columns == APPLICATION_SETTINGS_COLUMNS
    assert await _applied_migration_ids(database) == [
        migration.id for migration in MIGRATIONS
    ]
    assert integrity_check == "ok"
    assert settings is not None
    assert settings.logging_level == "INFO"
    assert settings.retention_days == 14
    assert settings.max_file_mb == 10
    assert settings.inspection_auto_start_enabled is True
    assert settings.revision == 0
    assert settings.content_policy_json == "{}"
    assert settings.managed_tmdb_key_encrypted is None
    assert settings.managed_tmdb_updated_at is None
    assert settings.managed_p115_cookie_encrypted is None
    assert settings.managed_p115_updated_at is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_initialize_database_upgrades_legacy_settings_without_losing_data(
    tmp_path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    now = datetime.now(UTC)
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE application_settings (
                id VARCHAR(16) PRIMARY KEY,
                logging_level VARCHAR(16) NOT NULL,
                retention_days INTEGER NOT NULL,
                max_file_mb INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        await connection.execute(
            text(
                """
                INSERT INTO application_settings
                    (id, logging_level, retention_days, max_file_mb, revision, updated_at)
                VALUES
                    (:id, :logging_level, :retention_days, :max_file_mb, :revision, :updated_at)
                """
            ),
            {
                "id": "default",
                "logging_level": "WARNING",
                "retention_days": 30,
                "max_file_mb": 50,
                "revision": 7,
                "updated_at": now,
            },
        )
        await connection.run_sync(
            lambda sync_connection: ApplicationSettings.metadata.create_all(
                sync_connection
            )
        )

    async with database.session_factory() as session:
        resource = Resource(
            id="res_legacy",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:legacy",
            encrypted_url="encrypted-url",
            name="Legacy resource",
            source="test",
            captured_at=now,
            expires_at=now + timedelta(days=1),
        )
        session.add_all(
            [
                resource,
                Task(
                    id="task_legacy",
                    resource=resource,
                    action=TaskAction.OFFLINE_DOWNLOAD,
                    encrypted_url_snapshot="encrypted-url",
                ),
                WebSession(
                    session_digest="session_legacy",
                    csrf_token="csrf",
                    credential_fingerprint="fingerprint",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                ),
            ]
        )
        await session.commit()

    await initialize_database(database.engine)

    async with database.session_factory() as session:
        settings = await session.get(ApplicationSettings, "default")
        resource = await session.get(Resource, "res_legacy")
        task = await session.get(Task, "task_legacy")
        web_session = await session.get(WebSession, "session_legacy")

    assert settings is not None
    assert settings.logging_level == "WARNING"
    assert settings.retention_days == 30
    assert settings.max_file_mb == 50
    assert settings.revision == 7
    assert settings.inspection_auto_start_enabled is True
    assert settings.content_policy_json == "{}"
    assert settings.managed_tmdb_key_encrypted is None
    assert settings.managed_tmdb_updated_at is None
    assert settings.managed_p115_cookie_encrypted is None
    assert settings.managed_p115_updated_at is None
    assert resource is not None and resource.name == "Legacy resource"
    assert task is not None and task.resource_id == "res_legacy"
    assert web_session is not None and web_session.csrf_token == "csrf"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_initialize_database_is_idempotent(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")

    await initialize_database(database.engine)
    await initialize_database(database.engine)

    assert await _applied_migration_ids(database) == [
        migration.id for migration in MIGRATIONS
    ]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_migrations_run_in_order_and_skip_applied_ids(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    executed: list[str] = []

    def apply_first(_connection) -> None:
        executed.append("first")

    def apply_second(_connection) -> None:
        executed.append("second")

    migrations = (
        Migration("001_first", apply_first),
        Migration("002_second", apply_second),
    )
    async with database.engine.begin() as connection:
        await connection.run_sync(run_migrations, migrations)
    async with database.engine.begin() as connection:
        await connection.run_sync(run_migrations, migrations)

    assert executed == ["first", "second"]
    assert await _applied_migration_ids(database) == ["001_first", "002_second"]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_migration_ids_are_rejected(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    migrations = (
        Migration("001_duplicate", lambda _connection: None),
        Migration("001_duplicate", lambda _connection: None),
    )

    with pytest.raises(ValueError, match="unique"):
        async with database.engine.begin() as connection:
            await connection.run_sync(run_migrations, migrations)

    await database.engine.dispose()


@pytest.mark.asyncio
async def test_failed_migration_is_not_recorded_and_can_be_retried(
    tmp_path,
    monkeypatch,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")

    def apply_first(connection) -> None:
        connection.execute(
            text("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")
        )

    def fail(_connection) -> None:
        raise RuntimeError("migration failed")

    failed_migrations = (
        Migration("001_create_probe", apply_first),
        Migration("002_fail", fail),
    )

    def run_failed_migrations(connection) -> None:
        run_migrations(connection, failed_migrations)

    monkeypatch.setattr(database_module, "run_migrations", run_failed_migrations)

    with pytest.raises(RuntimeError, match="migration failed"):
        await initialize_database(database.engine)

    async with database.engine.connect() as connection:
        has_migration_history = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).has_table(
                "schema_migrations"
            )
        )
        has_probe = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).has_table(
                "migration_probe"
            )
        )
    assert has_migration_history is False
    assert has_probe is False

    def apply_second(connection) -> None:
        connection.execute(
            text("CREATE TABLE migration_repaired (id INTEGER PRIMARY KEY)")
        )

    repaired_migrations = (
        Migration("001_create_probe", apply_first),
        Migration("002_fail", apply_second),
    )

    def run_repaired_migrations(connection) -> None:
        run_migrations(connection, repaired_migrations)

    monkeypatch.setattr(database_module, "run_migrations", run_repaired_migrations)
    await initialize_database(database.engine)

    assert await _applied_migration_ids(database) == [
        "001_create_probe",
        "002_fail",
    ]
    await database.engine.dispose()
