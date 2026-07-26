"""Async SQLite setup and retention cleanup."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, event, inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from watch_assistant.models import (
    Base,
    InspectionBatch,
    InspectionItem,
    MagnetMetadataCache,
    Resource,
    SearchCache,
    Task,
    TaskState,
)
from watch_assistant.schemas import InspectionBatchStatus, InspectionItemStatus


@dataclass(frozen=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class CleanupResult:
    resources_deleted: int
    tasks_deleted: int
    cache_entries_deleted: int
    inspection_batches_deleted: int


PROTECTED_TASK_STATES = (
    TaskState.QUEUED,
    TaskState.SUBMITTING,
    TaskState.NEEDS_AUTH,
    TaskState.UNCERTAIN,
)
TERMINAL_TASK_STATES = (TaskState.ACCEPTED, TaskState.FAILED)


def create_database(url: str) -> Database:
    engine = create_async_engine(url)

    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))


async def initialize_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_ensure_application_settings_columns)


def _ensure_application_settings_columns(connection) -> None:
    columns = {
        item["name"] for item in inspect(connection).get_columns("application_settings")
    }
    additions = {
        "inspection_auto_start_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "content_policy_json": "TEXT NOT NULL DEFAULT '{}'",
        "managed_tmdb_key_encrypted": "TEXT",
        "managed_tmdb_updated_at": "DATETIME",
        "managed_p115_cookie_encrypted": "TEXT",
        "managed_p115_updated_at": "DATETIME",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE application_settings ADD COLUMN {name} {definition}")
            )


async def cleanup_expired(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> CleanupResult:
    current_time = now or datetime.now(UTC)
    task_cutoff = current_time - timedelta(days=90)
    resource_cutoff = current_time - timedelta(days=30)

    task_result = await session.execute(
        delete(Task).where(
            Task.state.in_(TERMINAL_TASK_STATES),
            Task.updated_at < task_cutoff,
        )
    )

    protected_resource_ids = select(Task.resource_id).where(
        Task.resource_id.is_not(None), Task.state.in_(PROTECTED_TASK_STATES)
    )
    protected_inspection_resource_ids = (
        select(InspectionItem.resource_id)
        .join(InspectionBatch)
        .where(
            InspectionBatch.status.in_(
                (InspectionBatchStatus.QUEUED, InspectionBatchStatus.RUNNING)
            ),
            InspectionItem.status.in_(
                (InspectionItemStatus.QUEUED, InspectionItemStatus.RUNNING)
            ),
        )
    )
    resource_result = await session.execute(
        delete(Resource).where(
            Resource.created_at < resource_cutoff,
            Resource.expires_at <= current_time,
            Resource.id.not_in(protected_resource_ids),
            Resource.id.not_in(protected_inspection_resource_ids),
        )
    )
    cache_result = await session.execute(
        delete(SearchCache).where(SearchCache.expires_at <= current_time)
    )
    inspection_cache_result = await session.execute(
        delete(MagnetMetadataCache).where(
            MagnetMetadataCache.expires_at.is_not(None),
            MagnetMetadataCache.expires_at <= current_time,
        )
    )
    inspection_result = await session.execute(
        delete(InspectionBatch).where(InspectionBatch.expires_at <= current_time)
    )

    return CleanupResult(
        resources_deleted=resource_result.rowcount or 0,
        tasks_deleted=task_result.rowcount or 0,
        cache_entries_deleted=(cache_result.rowcount or 0)
        + (inspection_cache_result.rowcount or 0),
        inspection_batches_deleted=inspection_result.rowcount or 0,
    )
