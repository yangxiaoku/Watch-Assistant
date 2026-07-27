"""Versioned SQLite schema migrations."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

MigrationApply = Callable[[Connection], None]


@dataclass(frozen=True)
class Migration:
    """One ordered, forward-only schema migration."""

    id: str
    apply: MigrationApply


APPLICATION_SETTINGS_COLUMN_ADDITIONS = (
    ("inspection_auto_start_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("content_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("managed_tmdb_key_encrypted", "TEXT"),
    ("managed_tmdb_updated_at", "DATETIME"),
    ("managed_p115_cookie_encrypted", "TEXT"),
    ("managed_p115_updated_at", "DATETIME"),
)


def _add_application_settings_columns(connection: Connection) -> None:
    columns = {
        item["name"] for item in inspect(connection).get_columns("application_settings")
    }
    for name, definition in APPLICATION_SETTINGS_COLUMN_ADDITIONS:
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE application_settings ADD COLUMN {name} {definition}")
            )


def _create_library_index_tables(connection: Connection) -> None:
    """Create the forward-only read/index tables for legacy SQLite databases."""

    statements = (
        """
            CREATE TABLE IF NOT EXISTS media_libraries (
                id VARCHAR(128) PRIMARY KEY,
                name TEXT NOT NULL,
                root_directory_id VARCHAR(128) NOT NULL,
                scope_verified BOOLEAN NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL
            )
        """,
        """
            CREATE TABLE IF NOT EXISTS library_scan_runs (
                id VARCHAR(64) PRIMARY KEY,
                library_id VARCHAR(128) NOT NULL REFERENCES media_libraries(id),
                root_directory_id VARCHAR(128) NOT NULL,
                idempotency_key VARCHAR(128) NOT NULL,
                state VARCHAR(16) NOT NULL DEFAULT 'queued',
                complete BOOLEAN NOT NULL DEFAULT 0,
                snapshot_revision INTEGER,
                expected_page_count INTEGER,
                expected_total INTEGER,
                pages_read INTEGER NOT NULL DEFAULT 0,
                items_seen INTEGER NOT NULL DEFAULT 0,
                added_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                error_code VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (library_id, idempotency_key)
            )
        """,
        """
            CREATE TABLE IF NOT EXISTS library_scan_checkpoints (
                scan_run_id VARCHAR(64) PRIMARY KEY
                    REFERENCES library_scan_runs(id) ON DELETE CASCADE,
                page INTEGER NOT NULL DEFAULT 0,
                items_seen INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL
            )
        """,
        """
            CREATE TABLE IF NOT EXISTS library_scan_entries (
                scan_run_id VARCHAR(64) NOT NULL
                    REFERENCES library_scan_runs(id) ON DELETE CASCADE,
                object_type VARCHAR(16) NOT NULL,
                object_id VARCHAR(128) NOT NULL,
                parent_id VARCHAR(128),
                name TEXT NOT NULL,
                path TEXT,
                is_directory BOOLEAN NOT NULL,
                size_bytes BIGINT,
                modified_at DATETIME,
                PRIMARY KEY (scan_run_id, object_type, object_id)
            )
        """,
        """
            CREATE TABLE IF NOT EXISTS library_scan_diffs (
                scan_run_id VARCHAR(64) NOT NULL
                    REFERENCES library_scan_runs(id) ON DELETE CASCADE,
                object_type VARCHAR(16) NOT NULL,
                object_id VARCHAR(128) NOT NULL,
                change_kind VARCHAR(16) NOT NULL,
                path_changed BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (scan_run_id, object_type, object_id)
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS ix_library_scan_runs_library_id
                ON library_scan_runs (library_id)
        """,
    )
    for statement in statements:
        connection.execute(text(statement))


def _create_organization_plan_tables(connection: Connection) -> None:
    """Create local, preview-only organization plans for legacy databases."""

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS organization_plans (
                id VARCHAR(64) PRIMARY KEY,
                library_id VARCHAR(128) NOT NULL
                    REFERENCES media_libraries(id),
                source_scan_run_id VARCHAR(64) NOT NULL
                    REFERENCES library_scan_runs(id),
                source_snapshot_revision INTEGER NOT NULL,
                source_snapshot_json TEXT NOT NULL,
                target_root TEXT NOT NULL,
                actions_json TEXT NOT NULL,
                basis_json TEXT NOT NULL,
                preconditions_json TEXT NOT NULL,
                rule_version VARCHAR(64) NOT NULL,
                parser_version VARCHAR(64) NOT NULL,
                matcher_version VARCHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'needs_review',
                revision INTEGER NOT NULL DEFAULT 1,
                expires_at DATETIME NOT NULL,
                plan_hash VARCHAR(64) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_organization_plans_library_id
                ON organization_plans (library_id)
            """
        )
    )


def _add_organization_plan_alias(connection: Connection) -> None:
    """Add the local-only review alias without changing existing plans."""

    columns = {
        item["name"] for item in inspect(connection).get_columns("organization_plans")
    }
    if "alias" not in columns:
        connection.execute(text("ALTER TABLE organization_plans ADD COLUMN alias TEXT"))


MIGRATIONS: tuple[Migration, ...] = (
    Migration("001_application_settings_columns", _add_application_settings_columns),
    Migration("002_library_index_tables", _create_library_index_tables),
    Migration("003_organization_plan_tables", _create_organization_plan_tables),
    Migration("004_organization_plan_alias", _add_organization_plan_alias),
)


def run_migrations(
    connection: Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> None:
    """Apply pending migrations inside the caller's transaction."""
    _validate_migrations(migrations)
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied_ids = set(
        connection.execute(text("SELECT migration_id FROM schema_migrations")).scalars()
    )
    for migration in migrations:
        if migration.id in applied_ids:
            continue
        migration.apply(connection)
        connection.execute(
            text("INSERT INTO schema_migrations (migration_id) VALUES (:migration_id)"),
            {"migration_id": migration.id},
        )


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    migration_ids = [migration.id for migration in migrations]
    if not all(migration_ids):
        raise ValueError("Migration IDs must not be empty")
    if len(migration_ids) != len(set(migration_ids)):
        raise ValueError("Migration IDs must be unique")
    if migration_ids != sorted(migration_ids):
        raise ValueError("Migrations must be ordered by ID")
