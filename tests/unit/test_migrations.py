from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

import watch_assistant.db as database_module
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.migrations import MIGRATIONS, Migration, run_migrations
from watch_assistant.models import (
    ApplicationSettings,
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    OrganizationOperation,
    Resource,
    Task,
    WebSession,
)
from watch_assistant.schemas import ResourceKind, TaskAction, WorkflowCreateRequest
from watch_assistant.services.workflows import WorkflowService

APPLICATION_SETTINGS_COLUMNS = {
    "id",
    "logging_level",
    "retention_days",
    "max_file_mb",
    "inspection_auto_start_enabled",
    "revision",
    "content_policy_json",
    "organization_settings_json",
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
    assert settings.organization_settings_json == "{}"
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
async def test_dirty_generation_migration_backfills_unconsumed_events(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="legacy-library",
                name="legacy",
                root_directory_id="legacy-root",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="legacy-scan",
                library_id="legacy-library",
                root_directory_id="legacy-root",
                idempotency_key="legacy-scan-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            OrganizationPlan(
                id="legacy-plan",
                library_id="legacy-library",
                source_scan_run_id="legacy-scan",
                source_snapshot_revision=1,
                source_snapshot_json="{}",
                target_root="",
                actions_json="[]",
                basis_json="{}",
                preconditions_json="{}",
                rule_version="test-v1",
                parser_version="test-v1",
                matcher_version="test-v1",
                status="planned",
                revision=1,
                expires_at=now + timedelta(hours=1),
                plan_hash="legacy-plan-hash",
            )
        )
        await session.flush()
        session.add(
            OrganizationOperation(
                id="legacy-operation",
                plan_id="legacy-plan",
                plan_revision=1,
                idempotency_key="legacy-operation-key",
            )
        )
        await session.flush()
        session.add(
            DirectoryDirtyEvent(
                id="legacy-event",
                operation_id="legacy-operation",
                directory_id="legacy-directory",
                status="pending",
                created_at=now,
                updated_at=now,
                available_at=now,
            )
        )
        await session.commit()

    async with database.engine.begin() as connection:
        await connection.run_sync(
            lambda sync: DirectoryDirtyGeneration.__table__.drop(
                sync, checkfirst=True
            )
        )
        await connection.execute(
            text(
                "DELETE FROM schema_migrations "
                "WHERE migration_id = '047_directory_dirty_generations'"
            )
        )
        await connection.run_sync(
            run_migrations,
            tuple(
                migration
                for migration in MIGRATIONS
                if migration.id
                in {
                    "047_directory_dirty_generations",
                    "048_organization_operation_workflow",
                }
            ),
        )

    async with database.session_factory() as session:
        generation = await session.get(
            DirectoryDirtyGeneration, "gen_legacy_legacy-event"
        )
        assert generation is not None
        assert generation.library_id == "legacy-library"
        assert generation.directory_id == "legacy-directory"
        assert generation.generation == 1
        assert generation.status == "queued"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_organization_operation_migration_preserves_existing_data(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    async with database.engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT)")
        )
        await connection.execute(
            text("INSERT INTO legacy_marker (id, value) VALUES (1, 'preserved')")
        )
        await connection.run_sync(
            lambda sync: OrganizationOperation.__table__.drop(sync, checkfirst=True)
        )
        await connection.execute(
            text(
                "DELETE FROM schema_migrations "
                "WHERE migration_id IN ("
                "'005_organization_operations', '006_directory_dirty_outbox'"
                ")"
            )
        )
        await connection.run_sync(run_migrations, MIGRATIONS[4:])

    async with database.engine.connect() as connection:
        marker = await connection.scalar(text("SELECT value FROM legacy_marker"))
        has_operations = await connection.run_sync(
            lambda sync: inspect(sync).has_table(OrganizationOperation.__tablename__)
        )
    assert marker == "preserved"
    assert has_operations is True
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_running_operation_cancel_column_migrates_legacy_table(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE organization_operations (id VARCHAR(40) PRIMARY KEY)"
        )
        cancel_migration = tuple(
            migration
            for migration in MIGRATIONS
            if migration.id == "051_organization_cancel_requested"
        )
        await connection.run_sync(run_migrations, cancel_migration)
        columns = await connection.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync).get_columns("organization_operations")
            }
        )
    assert "cancel_requested" in columns
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_resource_search_workflow_column_migrates_legacy_table(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'search-legacy.db'}")
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE resource_search_jobs (task_id VARCHAR(64) PRIMARY KEY)"
        )
        await connection.run_sync(
            run_migrations,
            tuple(
                migration
                for migration in MIGRATIONS
                if migration.id == "052_resource_search_workflow"
            ),
        )
        columns = await connection.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync).get_columns("resource_search_jobs")
            }
        )
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("resource_search_jobs")
        )
    assert "workflow_id" in columns
    assert any(
        index["name"] == "ix_resource_search_jobs_workflow_id" for index in indexes
    )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_legacy_workflow_schema_is_upgraded_without_losing_rows(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE workflows (
                id VARCHAR(40) PRIMARY KEY,
                correlation_id VARCHAR(64) NOT NULL,
                status VARCHAR(21) NOT NULL,
                subscription_id VARCHAR(128),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            """
            CREATE TABLE workflow_stages (
                id VARCHAR(40) PRIMARY KEY,
                workflow_id VARCHAR(40) NOT NULL,
                stage_key VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL,
                task_id VARCHAR(40),
                finished_at DATETIME,
                updated_at DATETIME NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            "INSERT INTO workflows (id, correlation_id, status, created_at, updated_at) "
            "VALUES ('wf_legacy', 'corr_legacy', 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO workflow_stages "
            "(id, workflow_id, stage_key, status, task_id, finished_at, updated_at) "
            "VALUES ('stage_legacy', 'wf_legacy', 'search', 'completed', 'task_legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    await initialize_database(database.engine)

    async with database.engine.connect() as connection:
        workflow_columns = await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_columns("workflows")}
        )
        stage_columns = await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_columns("workflow_stages")}
        )
        stage = (
            await connection.execute(
                text("SELECT stage, sequence, status, child_type, child_id, completed_at, created_at FROM workflow_stages")
            )
        ).one()

    assert {"media_type", "tmdb_id", "state_reason"} <= workflow_columns
    assert "sequence" in stage_columns
    assert stage == ("discovery", 0, "succeeded", "task", "task_legacy", stage[5], stage[6])

    created = await WorkflowService(database.session_factory).create(
        WorkflowCreateRequest(media_type="movie")
    )
    assert created.id.startswith("wf_")
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
async def test_audit_schema_compatibility_migration_preserves_legacy_columns(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'audit-legacy.db'}")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE audit_records ("
                "id VARCHAR(64) PRIMARY KEY, event_code VARCHAR(128), "
                "event_version INTEGER, action TEXT, outcome TEXT, "
                "actor_type TEXT, actor_id TEXT, request_id TEXT, "
                "correlation_id TEXT, task_id TEXT, resource_type TEXT, "
                "resource_id TEXT, error_code TEXT, details_json TEXT, "
                "created_at DATETIME, expires_at DATETIME)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO audit_records "
                "(id, event_code, created_at) VALUES ('legacy', 'legacy.event', "
                "'2026-07-29 00:00:00')"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE schema_migrations ("
                "migration_id TEXT PRIMARY KEY, "
                "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO schema_migrations (migration_id, applied_at) "
                "VALUES ('007_audit_records', CURRENT_TIMESTAMP)"
            )
        )
        compatibility = next(
            migration
            for migration in MIGRATIONS
            if migration.id == "037_audit_records_schema_compatibility"
        )
        await connection.run_sync(lambda sync: run_migrations(sync, (compatibility,)))
        columns = await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_columns("audit_records")}
        )
        row = (
            await connection.execute(
                text("SELECT timestamp, context_json FROM audit_records WHERE id='legacy'")
            )
        ).one()
    assert {"timestamp", "title_zh", "message_zh", "context_json"} <= columns
    assert row.timestamp == "2026-07-29 00:00:00"
    assert row.context_json == "{}"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_legacy_audit_rebuild_keeps_backup_and_allows_current_insert(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'audit-rebuild.db'}")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE audit_records ("
                "id VARCHAR(64) PRIMARY KEY, event_code VARCHAR(128) NOT NULL, "
                "event_version INTEGER NOT NULL, action TEXT NOT NULL, "
                "outcome TEXT NOT NULL, actor_type TEXT, actor_id TEXT, "
                "request_id TEXT, correlation_id TEXT, task_id TEXT, "
                "resource_type TEXT, resource_id TEXT, error_code TEXT, "
                "details_json TEXT, created_at DATETIME, expires_at DATETIME)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO audit_records "
                "(id, event_code, event_version, action, outcome) "
                "VALUES ('legacy', 'legacy.event', 1, 'old-action', 'old-outcome')"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, "
                "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        migrations = tuple(
            migration
            for migration in MIGRATIONS
            if migration.id in {
                "037_audit_records_schema_compatibility",
                "038_audit_records_legacy_rebuild",
            }
        )
        await connection.run_sync(lambda sync: run_migrations(sync, migrations))
        await connection.execute(
            text(
                "INSERT INTO audit_records "
                "(id, timestamp, event_code, event_version, title_zh, message_zh, "
                "status, actor_type, actor_id, context_json) VALUES "
                "('current', CURRENT_TIMESTAMP, 'current.event', 1, '标题', '消息', "
                "'completed', 'system', 'test', '{}')"
            )
        )
        backup_count = await connection.scalar(
            text("SELECT count(*) FROM audit_records_legacy_038")
        )
        current_count = await connection.scalar(text("SELECT count(*) FROM audit_records"))
    assert backup_count == 1
    assert current_count == 2
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
