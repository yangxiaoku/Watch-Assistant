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


MIGRATIONS: tuple[Migration, ...] = (
    Migration("001_application_settings_columns", _add_application_settings_columns),
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
