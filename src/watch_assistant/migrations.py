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
    ("managed_prowlarr_enabled", "BOOLEAN"),
    ("managed_prowlarr_base_url", "TEXT"),
    ("managed_prowlarr_api_key_encrypted", "TEXT"),
    ("managed_prowlarr_updated_at", "DATETIME"),
)

PROWLARR_APPLICATION_SETTINGS_COLUMN_ADDITIONS = (
    ("managed_prowlarr_enabled", "BOOLEAN"),
    ("managed_prowlarr_base_url", "TEXT"),
    ("managed_prowlarr_api_key_encrypted", "TEXT"),
    ("managed_prowlarr_updated_at", "DATETIME"),
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


def _add_prowlarr_application_settings_columns(connection: Connection) -> None:
    """Repair databases that recorded migration 001 before Prowlarr existed."""

    if not inspect(connection).has_table("application_settings"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("application_settings")
    }
    for name, definition in PROWLARR_APPLICATION_SETTINGS_COLUMN_ADDITIONS:
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE application_settings ADD COLUMN {name} {definition}")
            )


def _add_organization_settings_column(connection: Connection) -> None:
    columns = {
        item["name"] for item in inspect(connection).get_columns("application_settings")
    }
    if "organization_settings_json" not in columns:
        connection.execute(
            text(
                "ALTER TABLE application_settings "
                "ADD COLUMN organization_settings_json TEXT NOT NULL DEFAULT '{}'"
            )
        )


def _create_p115_login_devices_table(connection: Connection) -> None:
    from watch_assistant.models import P115LoginDevice

    P115LoginDevice.__table__.create(connection, checkfirst=True)


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
                pickcode TEXT,
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


def _create_library_media_identity_table(connection: Connection) -> None:
    """Persist local TMDB confirmations without changing remote files."""

    from watch_assistant.library_models import LibraryMediaIdentity

    LibraryMediaIdentity.__table__.create(connection, checkfirst=True)


def _create_library_inventory_ledger_tables(connection: Connection) -> None:
    """Persist complete-scan availability evidence and recovery events."""

    from watch_assistant.library_models import (
        LibraryInventoryEvent,
        LibraryObjectLedger,
    )

    LibraryObjectLedger.__table__.create(connection, checkfirst=True)
    LibraryInventoryEvent.__table__.create(connection, checkfirst=True)
    scan_columns = {
        item["name"] for item in inspect(connection).get_columns("library_scan_runs")
    }
    if "removed_count" not in scan_columns:
        connection.execute(
            text(
                "ALTER TABLE library_scan_runs "
                "ADD COLUMN removed_count INTEGER NOT NULL DEFAULT 0"
            )
        )


def _upgrade_library_scan_lifecycle(connection: Connection) -> None:
    """Add durable worker leases and tree cursors to existing scan tables."""

    inspector = inspect(connection)
    if not inspector.has_table("library_scan_runs"):
        return
    run_columns = {
        item["name"] for item in inspector.get_columns("library_scan_runs")
    }
    run_additions = (
        ("scan_mode", "VARCHAR(16) NOT NULL DEFAULT 'tree'"),
        ("max_directories", "INTEGER NOT NULL DEFAULT 10000"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("lease_owner", "VARCHAR(128)"),
        ("lease_token", "VARCHAR(64)"),
        ("lease_expires_at", "DATETIME"),
        ("cancel_requested", "BOOLEAN NOT NULL DEFAULT 0"),
    )
    for name, definition in run_additions:
        if name not in run_columns:
            connection.execute(
                text(f"ALTER TABLE library_scan_runs ADD COLUMN {name} {definition}")
            )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_library_scan_runs_lease "
            "ON library_scan_runs (state, lease_expires_at, created_at)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_library_scan_run_idempotency "
            "ON library_scan_runs (library_id, idempotency_key)"
        )
    )

    if not inspector.has_table("library_scan_checkpoints"):
        return
    checkpoint_columns = {
        item["name"]
        for item in inspect(connection).get_columns("library_scan_checkpoints")
    }
    if "cursor_json" not in checkpoint_columns:
        connection.execute(
            text(
                "ALTER TABLE library_scan_checkpoints "
                "ADD COLUMN cursor_json TEXT NOT NULL DEFAULT '{}'"
            )
        )


def _invalidate_legacy_library_scan_modes(connection: Connection) -> None:
    """Require a fresh recursive scan after scan mode became authoritative."""

    if not inspect(connection).has_table("library_scan_runs"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("library_scan_runs")
    }
    if "scan_mode" in columns:
        connection.execute(
            text(
                "UPDATE library_scan_runs "
                "SET scan_mode = 'legacy' "
                "WHERE scan_mode = 'tree'"
            )
        )


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


def _create_organization_operation_table(connection: Connection) -> None:
    """Create the local operation ledger without adding execution wiring."""

    from watch_assistant.models import OrganizationOperation

    OrganizationOperation.__table__.create(connection, checkfirst=True)


def _create_directory_dirty_outbox_table(connection: Connection) -> None:
    """Create the pending local directory dirty-event outbox."""

    from watch_assistant.models import DirectoryDirtyEvent

    DirectoryDirtyEvent.__table__.create(connection, checkfirst=True)


def _upgrade_directory_dirty_outbox(connection: Connection) -> None:
    """Add durable lease and retry state to existing dirty events."""

    if not inspect(connection).has_table("directory_dirty_events"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("directory_dirty_events")
    }
    additions = (
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("lease_token", "VARCHAR(64)"),
        ("lease_expires_at", "DATETIME"),
        ("available_at", "DATETIME"),
        ("error_code", "VARCHAR(64)"),
        ("updated_at", "DATETIME"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE directory_dirty_events ADD COLUMN {name} {definition}")
            )
    connection.execute(
        text(
            "UPDATE directory_dirty_events "
            "SET available_at = COALESCE(available_at, created_at), "
            "updated_at = COALESCE(updated_at, created_at)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_directory_dirty_events_due "
            "ON directory_dirty_events (status, available_at, lease_expires_at)"
        )
    )


def _create_directory_dirty_generation_table(connection: Connection) -> None:
    """Create the coalesced directory queue used by incremental reconciliation."""

    from watch_assistant.models import DirectoryDirtyGeneration

    DirectoryDirtyGeneration.__table__.create(connection, checkfirst=True)
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_directory_dirty_generations_due "
            "ON directory_dirty_generations (status, available_at, lease_expires_at)"
        )
    )
    if not inspect(connection).has_table("directory_dirty_events"):
        return
    connection.execute(
        text(
            """
            INSERT OR IGNORE INTO directory_dirty_generations (
                id, library_id, directory_id, operation_id, generation,
                claimed_generation, status, attempts, lease_token,
                lease_expires_at, available_at, error_code, created_at, updated_at
            )
            WITH legacy_events AS (
                SELECT
                    event.id,
                    plan.library_id,
                    event.directory_id,
                    event.operation_id,
                    event.status,
                    event.attempts,
                    event.lease_token,
                    event.lease_expires_at,
                    event.available_at,
                    event.error_code,
                    event.created_at,
                    event.updated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY plan.library_id, event.directory_id
                        ORDER BY event.created_at DESC, event.id DESC
                    ) AS event_rank,
                    COUNT(*) OVER (
                        PARTITION BY plan.library_id, event.directory_id
                    ) AS generation_count
                FROM directory_dirty_events AS event
                JOIN organization_operations AS operation
                  ON operation.id = event.operation_id
                JOIN organization_plans AS plan
                  ON plan.id = operation.plan_id
                WHERE event.event_kind = 'directory_dirty'
                  AND event.status IN ('pending', 'running')
            )
            SELECT
                'gen_legacy_' || id,
                library_id,
                directory_id,
                operation_id,
                generation_count,
                CASE WHEN status = 'running' THEN generation_count ELSE NULL END,
                CASE WHEN status = 'running' THEN 'running' ELSE 'queued' END,
                COALESCE(attempts, 0),
                lease_token,
                lease_expires_at,
                COALESCE(available_at, created_at),
                error_code,
                created_at,
                COALESCE(updated_at, created_at)
            FROM legacy_events
            WHERE event_rank = 1
            """
        )
    )


def _add_organization_operation_workflow(connection: Connection) -> None:
    """Add the optional top-level workflow link to organization operations."""

    if not inspect(connection).has_table("organization_operations"):
        return
    columns = {
        item["name"]
        for item in inspect(connection).get_columns("organization_operations")
    }
    if "workflow_id" not in columns:
        connection.execute(
            text(
                "ALTER TABLE organization_operations "
                "ADD COLUMN workflow_id VARCHAR(40)"
            )
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_organization_operations_workflow_id "
            "ON organization_operations (workflow_id)"
        )
    )


def _create_audit_records_table(connection: Connection) -> None:
    """Create durable security audit storage for existing installations."""

    from watch_assistant.models import AuditRecord

    AuditRecord.__table__.create(connection, checkfirst=True)


def _create_strm_manifest_table(connection: Connection) -> None:
    """Create the local STRM manifest without touching remote 115 state."""

    from watch_assistant.library_models import StrmManifestEntry

    StrmManifestEntry.__table__.create(connection, checkfirst=True)


def _create_strm_operations_table(connection: Connection) -> None:
    """Create the durable ledger for STRM synchronization requests."""

    from watch_assistant.models import StrmOperation

    StrmOperation.__table__.create(connection, checkfirst=True)


def _add_strm_cleanup_idempotency(connection: Connection) -> None:
    """Persist the result key for safe retries after a successful cleanup."""

    if not inspect(connection).has_table("strm_cleanup_plans"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("strm_cleanup_plans")
    }
    if "applied_idempotency_key" not in columns:
        connection.execute(
            text(
                "ALTER TABLE strm_cleanup_plans "
                "ADD COLUMN applied_idempotency_key VARCHAR(128)"
            )
        )
    if "applied_retired" not in columns:
        connection.execute(
            text(
                "ALTER TABLE strm_cleanup_plans "
                "ADD COLUMN applied_retired INTEGER NOT NULL DEFAULT 0"
            )
        )


def _add_strm_operation_leases(connection: Connection) -> None:
    """Add forward-compatible lease state for long-running STRM work."""

    if not inspect(connection).has_table("strm_operations"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("strm_operations")
    }
    for name, definition in (
        ("lease_owner", "VARCHAR(100)"),
        ("lease_expires_at", "DATETIME"),
        ("heartbeat_at", "DATETIME"),
    ):
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE strm_operations ADD COLUMN {name} {definition}")
            )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_strm_operations_lease_expires_at "
            "ON strm_operations (lease_expires_at)"
        )
    )


def _upgrade_audit_records_schema(connection: Connection) -> None:
    """Bridge the pre-REQ-004 audit table to the current event schema."""

    if not inspect(connection).has_table("audit_records"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("audit_records")
    }
    additions = (
        ("timestamp", "DATETIME"),
        ("title_zh", "TEXT"),
        ("message_zh", "TEXT"),
        ("suggestion_zh", "TEXT"),
        ("status", "VARCHAR(32)"),
        ("context_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE audit_records ADD COLUMN {name} {definition}")
            )
    if "created_at" in columns:
        connection.execute(
            text(
                "UPDATE audit_records SET timestamp = COALESCE(timestamp, created_at) "
                "WHERE timestamp IS NULL"
            )
        )
    connection.execute(
        text(
            "UPDATE audit_records SET context_json = '{}' "
            "WHERE context_json IS NULL"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_audit_records_timestamp "
            "ON audit_records (timestamp)"
        )
    )


def _rebuild_legacy_audit_records(connection: Connection) -> None:
    """Replace a legacy NOT NULL audit table while retaining a local copy."""

    if not inspect(connection).has_table("audit_records"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("audit_records")
    }
    if "action" not in columns or "outcome" not in columns:
        return
    info = {
        item["name"]: item
        for item in inspect(connection).get_columns("audit_records")
    }
    if not (info["action"].get("nullable") is False or info["outcome"].get("nullable") is False):
        return

    from watch_assistant.models import AuditRecord

    legacy_table = "audit_records_legacy_038"
    if not inspect(connection).has_table(legacy_table):
        connection.execute(
            text(
                f"CREATE TABLE {legacy_table} AS "
                "SELECT * FROM audit_records"
            )
        )
    for index_name in (
        "ix_audit_records_timestamp",
        "ix_audit_records_event_code",
        "ix_audit_records_status",
        "ix_audit_records_request_id",
        "ix_audit_records_correlation_id",
    ):
        connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    connection.execute(text("DROP TABLE audit_records"))
    AuditRecord.__table__.create(connection)
    connection.execute(
        text(
            f"""
            INSERT INTO audit_records (
                id, timestamp, event_code, event_version, title_zh, message_zh,
                suggestion_zh, status, request_id, correlation_id, actor_type,
                actor_id, resource_type, resource_id, task_id, context_json
            )
            SELECT
                id,
                COALESCE(timestamp, created_at, CURRENT_TIMESTAMP),
                event_code,
                COALESCE(event_version, 1),
                COALESCE(action, '历史审计记录'),
                COALESCE(outcome, '历史审计记录'),
                NULL,
                outcome,
                request_id,
                correlation_id,
                actor_type,
                actor_id,
                resource_type,
                resource_id,
                task_id,
                COALESCE(NULLIF(details_json, ''), '{{}}')
            FROM {legacy_table}
            """
        )
    )


def _add_search_cache_kind(connection: Connection) -> None:
    columns = {
        item["name"] for item in inspect(connection).get_columns("search_cache")
    }
    if "cache_kind" not in columns:
        connection.execute(
            text(
                "ALTER TABLE search_cache ADD COLUMN cache_kind "
                "VARCHAR(16) NOT NULL DEFAULT 'positive'"
            )
        )


def _add_inspection_result_source(connection: Connection) -> None:
    for table in ("inspection_items", "magnet_metadata_cache"):
        columns = {item["name"] for item in inspect(connection).get_columns(table)}
        if "result_source" not in columns:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN result_source VARCHAR(32)")
            )


def _create_subscriptions_table(connection: Connection) -> None:
    from watch_assistant.models import Subscription

    Subscription.__table__.create(connection, checkfirst=True)


def _create_subscription_resource_observations_table(connection: Connection) -> None:
    """Persist canonical resources already seen by each subscription."""

    from watch_assistant.models import SubscriptionResourceObservation

    SubscriptionResourceObservation.__table__.create(connection, checkfirst=True)


def _add_subscription_episode_columns(connection: Connection) -> None:
    """Add the episode-range columns to subscriptions from older releases.

    ``010_subscriptions`` only creates the table when it does not yet exist, so
    subscriptions created before the episode columns were added to the model
    would never receive them, making every subscription query fail with
    ``no such column: subscriptions.episode_start``.
    """

    columns = {item["name"] for item in inspect(connection).get_columns("subscriptions")}
    for name, definition in (
        ("episode_start", "INTEGER"),
        ("episode_end", "INTEGER"),
        ("last_match_count", "INTEGER"),
    ):
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
            )


def _add_quality_profile_scope_columns(connection: Connection) -> None:
    """Add the scope columns to quality_profiles from older releases.

    ``011_quality_profiles`` only creates the table with ``checkfirst``, so a
    table created before the scope columns were added to the model never
    received them, making every quality profile query fail with
    ``no such column: quality_profiles.scope``.
    """

    columns = {item["name"] for item in inspect(connection).get_columns("quality_profiles")}
    for name, definition in (
        ("scope", "VARCHAR(16) NOT NULL DEFAULT 'global'"),
        ("scope_key", "VARCHAR(128)"),
        ("rules_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            connection.execute(
                text(f"ALTER TABLE quality_profiles ADD COLUMN {name} {definition}")
            )


def _add_subscription_observation_seen_count(connection: Connection) -> None:
    """Add the seen_count column to observation rows from older releases."""

    columns = {
        item["name"]
        for item in inspect(connection).get_columns("subscription_resource_observations")
    }
    if "seen_count" not in columns:
        connection.execute(
            text(
                "ALTER TABLE subscription_resource_observations "
                "ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 1"
            )
        )


def _repair_subscription_legacy_mode(connection: Connection) -> None:
    """Map the retired 'notify' subscription mode to its successor.

    Older releases stored ``mode='notify'``; the enum now only allows
    ``remind``/``confirm``/``auto``, so reading such a row raises
    ``LookupError`` and every subscription query returns HTTP 500.
    ``remind`` is the closest behavioural successor and matches the default.
    """

    connection.execute(
        text("UPDATE subscriptions SET mode = 'remind' WHERE mode = 'notify'")
    )


def _add_subscription_partial_unique_indexes(connection: Connection) -> None:
    """并发创建同一电影订阅时可插入重复行。

    ``uq_subscription_scope`` 唯一约束含可空列,SQLite 中 NULL 互不冲突,
    IntegrityError 兜底对电影场景(季节字段全 NULL)不触发。用部分唯一
    索引补上语义:电影按 (tmdb_id, media_type),剧集按完整五列。
    建索引前先清理既有重复,避免建索引失败。
    """

    connection.execute(
        text(
            "DELETE FROM subscriptions WHERE id NOT IN ("
            "  SELECT MIN(id) FROM subscriptions "
            "  GROUP BY tmdb_id, media_type, season_number, episode_start, episode_end"
            ")"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_scope_movie "
            "ON subscriptions (tmdb_id, media_type) WHERE season_number IS NULL"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_scope_tv "
            "ON subscriptions (tmdb_id, media_type, season_number, episode_start, episode_end) "
            "WHERE season_number IS NOT NULL"
        )
    )


def _add_organization_operation_partial_unique_index(connection: Connection) -> None:
    """允许终态操作不再锁死计划,失败后可对同一计划重试。

    旧约束 ``uq_organization_operations_plan_id`` 是整列唯一,任何已存在的
    操作(哪怕是 failed / cancelled / organized 终态)都会让新建操作报
    ``operation_plan_conflict``。改为部分唯一索引:仅当计划已有活跃操作
    (planned / organizing / uncertain)时阻塞,终态行可与新操作共存。
    状态列表须与
    ``services.organization_operations._ACTIVE_OPERATION_STATUSES`` 保持一致。
    """

    if not inspect(connection).has_table("organization_operations"):
        return
    for index in inspect(connection).get_indexes("organization_operations"):
        if (
            index["unique"]
            and index["column_names"] == ["plan_id"]
            and index["name"] != "uq_organization_operations_active_plan"
        ):
            connection.execute(
                text(f'DROP INDEX "{index["name"]}"')
            )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_organization_operations_active_plan "
            "ON organization_operations (plan_id) "
            "WHERE status IN ('planned', 'organizing', 'uncertain')"
        )
    )


def _repair_subscription_null_match_count(connection: Connection) -> None:
    """Replace NULL match counts left by legacy rows with the integer default.

    ``last_match_count`` is ``Integer, default=0`` in the model; a legacy NULL
    value fails the response schema's ``int`` validation and turns the whole
    subscription listing into an HTTP 500.
    """

    connection.execute(
        text(
            "UPDATE subscriptions SET last_match_count = 0 "
            "WHERE last_match_count IS NULL"
        )
    )


def _create_quality_profiles_table(connection: Connection) -> None:
    from watch_assistant.models import QualityProfile

    QualityProfile.__table__.create(connection, checkfirst=True)


def _create_workflow_tables(connection: Connection) -> None:
    from watch_assistant.models import Workflow, WorkflowStage

    Workflow.__table__.create(connection, checkfirst=True)
    WorkflowStage.__table__.create(connection, checkfirst=True)
    for table in ("tasks", "inspection_batches"):
        columns = {item["name"] for item in inspect(connection).get_columns(table)}
        if "workflow_id" not in columns:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN workflow_id VARCHAR(40)")
            )


def _create_notification_tables(connection: Connection) -> None:
    from watch_assistant.models import Notification, NotificationPreference

    Notification.__table__.create(connection, checkfirst=True)
    NotificationPreference.__table__.create(connection, checkfirst=True)


def _add_notification_quiet_hours(connection: Connection) -> None:
    columns = {
        item["name"] for item in inspect(connection).get_columns("notification_preferences")
    }
    additions = (
        ("quiet_hours_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
        ("quiet_hours_start", "VARCHAR(5) NOT NULL DEFAULT '23:00'"),
        ("quiet_hours_end", "VARCHAR(5) NOT NULL DEFAULT '08:00'"),
        ("quiet_hours_timezone", "VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'"),
        ("error_bypass_quiet_hours", "BOOLEAN NOT NULL DEFAULT 1"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(
                text(
                    "ALTER TABLE notification_preferences "
                    f"ADD COLUMN {name} {definition}"
                )
            )


def _create_strm_cleanup_plan_table(connection: Connection) -> None:
    """Persist non-destructive STRM cleanup previews for later review."""

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS strm_cleanup_plans (
                id VARCHAR(64) PRIMARY KEY,
                library_id VARCHAR(128) NOT NULL
                    REFERENCES media_libraries(id),
                source_scan_run_id VARCHAR(64) NOT NULL
                    REFERENCES library_scan_runs(id),
                source_snapshot_revision INTEGER NOT NULL,
                candidates_json TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS ix_strm_cleanup_plans_library_id
                ON strm_cleanup_plans (library_id)
            """
        )
    )


def _create_empty_directory_cleanup_plan_table(connection: Connection) -> None:
    """Persist reviewed, reversible empty-directory cleanup previews."""

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS empty_directory_cleanup_plans (
                id VARCHAR(64) PRIMARY KEY,
                library_id VARCHAR(128) NOT NULL
                    REFERENCES media_libraries(id),
                source_scan_run_id VARCHAR(64) NOT NULL
                    REFERENCES library_scan_runs(id),
                source_snapshot_revision INTEGER NOT NULL,
                candidates_json TEXT NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'needs_review',
                revision INTEGER NOT NULL DEFAULT 1,
                expires_at DATETIME NOT NULL,
                plan_hash VARCHAR(64) NOT NULL UNIQUE,
                applied_idempotency_key VARCHAR(128),
                applied_deleted INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_empty_directory_cleanup_plans_library_id
                ON empty_directory_cleanup_plans (library_id)
            """
        )
    )


def _create_managed_directory_ownership_table(connection: Connection) -> None:
    """Persist explicit ownership proof for directories created by organization."""

    from watch_assistant.library_models import ManagedDirectoryOwnership

    ManagedDirectoryOwnership.__table__.create(connection, checkfirst=True)
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_managed_directory_ownership_active "
            "ON managed_directory_ownership (library_id, status, directory_id)"
        )
    )


def _add_organization_cancel_requested(connection: Connection) -> None:
    """Add a durable, local cancellation request for running operations."""

    if not inspect(connection).has_table("organization_operations"):
        return
    columns = {
        item["name"]
        for item in inspect(connection).get_columns("organization_operations")
    }
    if "cancel_requested" not in columns:
        connection.execute(
            text(
                "ALTER TABLE organization_operations "
                "ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0"
            )
        )


def _create_season_metadata_cache_table(connection: Connection) -> None:
    from watch_assistant.models import SeasonMetadataCache

    SeasonMetadataCache.__table__.create(connection, checkfirst=True)


def _create_resource_search_jobs_table(connection: Connection) -> None:
    from watch_assistant.models import ResourceSearchJob

    ResourceSearchJob.__table__.create(connection, checkfirst=True)


def _create_agent_tokens_table(connection: Connection) -> None:
    from watch_assistant.models import AgentToken

    AgentToken.__table__.create(connection, checkfirst=True)


def _create_webhook_tables(connection: Connection) -> None:
    from watch_assistant.models import WebhookDelivery, WebhookEndpoint

    WebhookEndpoint.__table__.create(connection, checkfirst=True)
    WebhookDelivery.__table__.create(connection, checkfirst=True)
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_due "
            "ON webhook_deliveries (status, next_attempt_at)"
        )
    )


def _create_pwa_device_table(connection: Connection) -> None:
    from watch_assistant.models import PwaDevice

    PwaDevice.__table__.create(connection, checkfirst=True)


def _upgrade_legacy_workflow_schema(connection: Connection) -> None:
    """Bridge the earlier workflow tables to the current timeline model.

    Older releases used ``stage_key``/``task_id`` and recorded ``completed`` or
    ``queued`` stage states.  Keep those columns for existing callers, while
    adding the current names and normalizing values used by the ORM enum.
    """

    workflow_columns = {
        item["name"] for item in inspect(connection).get_columns("workflows")
    }
    for name, definition in (
        ("media_type", "VARCHAR(16)"),
        ("tmdb_id", "INTEGER"),
        ("state_reason", "TEXT"),
    ):
        if name not in workflow_columns:
            connection.execute(
                text(f"ALTER TABLE workflows ADD COLUMN {name} {definition}")
            )

    stage_columns = {
        item["name"] for item in inspect(connection).get_columns("workflow_stages")
    }
    for name, definition in (
        ("stage", "VARCHAR(32)"),
        ("child_type", "VARCHAR(64)"),
        ("child_id", "VARCHAR(128)"),
        ("completed_at", "DATETIME"),
    ):
        if name not in stage_columns:
            connection.execute(
                text(f"ALTER TABLE workflow_stages ADD COLUMN {name} {definition}")
            )

    if "stage_key" in stage_columns:
        connection.execute(
            text(
                """
                UPDATE workflow_stages
                SET stage = CASE stage_key
                    WHEN 'search' THEN 'discovery'
                    ELSE stage_key
                END
                WHERE stage IS NULL
                """
            )
        )
    connection.execute(
        text(
            """
            UPDATE workflow_stages
            SET status = CASE status
                WHEN 'completed' THEN 'succeeded'
                WHEN 'queued' THEN 'pending'
                WHEN 'needs_auth' THEN 'waiting_confirmation'
                ELSE status
            END
            WHERE status IN ('completed', 'queued', 'needs_auth')
            """
        )
    )
    if "task_id" in stage_columns:
        connection.execute(
            text(
                """
                UPDATE workflow_stages
                SET child_type = 'task', child_id = task_id
                WHERE child_id IS NULL AND task_id IS NOT NULL
                """
            )
        )
    if "finished_at" in stage_columns:
        connection.execute(
            text(
                """
                UPDATE workflow_stages
                SET completed_at = finished_at
                WHERE completed_at IS NULL AND finished_at IS NOT NULL
                """
            )
        )


def _upgrade_workflow_schema_compatibility_followup(connection: Connection) -> None:
    """Add current stage columns for databases that already ran migration 031."""

    stage_columns = {
        item["name"] for item in inspect(connection).get_columns("workflow_stages")
    }
    for name, definition in (
        ("reason", "TEXT"),
        ("error_code", "VARCHAR(100)"),
        ("started_at", "DATETIME"),
    ):
        if name not in stage_columns:
            connection.execute(
                text(f"ALTER TABLE workflow_stages ADD COLUMN {name} {definition}")
            )


def _upgrade_workflow_stage_sequence(connection: Connection) -> None:
    """Make inserts compatible with the older ordered stage table."""

    stage_columns = {
        item["name"] for item in inspect(connection).get_columns("workflow_stages")
    }
    if "sequence" not in stage_columns:
        connection.execute(
            text(
                "ALTER TABLE workflow_stages "
                "ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0"
            )
        )
    connection.execute(
        text(
            """
            UPDATE workflow_stages
            SET sequence = CASE COALESCE(stage, stage_key)
                WHEN 'discovery' THEN 0
                WHEN 'search' THEN 0
                WHEN 'inspection' THEN 1
                WHEN 'approval' THEN 2
                WHEN 'push' THEN 3
                WHEN 'availability' THEN 4
                WHEN 'organization' THEN 5
                WHEN 'strm' THEN 6
                ELSE sequence
            END
            WHERE sequence = 0
            """
        )
    )


def _upgrade_workflow_stage_created_at(connection: Connection) -> None:
    """Add the creation timestamp required by the ordered stage table."""

    stage_columns = {
        item["name"] for item in inspect(connection).get_columns("workflow_stages")
    }
    if "created_at" not in stage_columns:
        connection.execute(
            text("ALTER TABLE workflow_stages ADD COLUMN created_at DATETIME")
        )
    connection.execute(
        text(
            """
            UPDATE workflow_stages
            SET created_at = COALESCE(created_at, updated_at, CURRENT_TIMESTAMP)
            WHERE created_at IS NULL
            """
        )
    )


def _add_task_target_directory(connection: Connection) -> None:
    """Persist the user-selected P115 destination for queued push tasks."""

    if not inspect(connection).has_table("tasks"):
        return
    columns = {item["name"] for item in inspect(connection).get_columns("tasks")}
    if "target_directory_id" not in columns:
        connection.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN target_directory_id "
                "VARCHAR(128)"
            )
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_target_directory_id "
            "ON tasks (target_directory_id)"
        )
    )


def _add_library_scan_pickcode(connection: Connection) -> None:
    """Persist the protected playback identity on library scan snapshots."""

    if not inspect(connection).has_table("library_scan_entries"):
        return
    columns = {
        item["name"] for item in inspect(connection).get_columns("library_scan_entries")
    }
    if "pickcode" not in columns:
        connection.execute(
            text("ALTER TABLE library_scan_entries ADD COLUMN pickcode TEXT")
        )


def _add_task_lease_token(connection: Connection) -> None:
    """Add a fencing token so an expired worker cannot write back later."""

    if not inspect(connection).has_table("tasks"):
        return
    columns = {item["name"] for item in inspect(connection).get_columns("tasks")}
    if "lease_token" not in columns:
        connection.execute(
            text("ALTER TABLE tasks ADD COLUMN lease_token VARCHAR(64)")
        )


def _create_organization_history_table(connection: Connection) -> None:
    """Persist one row per media item completed by an organization operation."""

    from watch_assistant.library_models import OrganizationHistoryEntry

    OrganizationHistoryEntry.__table__.create(connection, checkfirst=True)


def _create_workflow_evidence_table(connection: Connection) -> None:
    """Persist safe read-only receipts used by workflow stage producers."""

    if inspect(connection).has_table("tasks"):
        connection.execute(
            text("UPDATE tasks SET status = 'submitted' WHERE status = 'accepted'")
        )
    from watch_assistant.models import WorkflowEvidence

    WorkflowEvidence.__table__.create(connection, checkfirst=True)


MIGRATIONS: tuple[Migration, ...] = (
    Migration("001_application_settings_columns", _add_application_settings_columns),
    Migration("002_library_index_tables", _create_library_index_tables),
    Migration("003_organization_plan_tables", _create_organization_plan_tables),
    Migration("004_organization_plan_alias", _add_organization_plan_alias),
    Migration("005_organization_operations", _create_organization_operation_table),
    Migration("006_directory_dirty_outbox", _create_directory_dirty_outbox_table),
    Migration("007_audit_records", _create_audit_records_table),
    Migration("008_search_cache_kind", _add_search_cache_kind),
    Migration("009_inspection_result_source", _add_inspection_result_source),
    Migration("010_subscriptions", _create_subscriptions_table),
    Migration("011_quality_profiles", _create_quality_profiles_table),
    Migration("012_workflow_tables", _create_workflow_tables),
    Migration("013_notification_tables", _create_notification_tables),
    Migration("014_season_metadata_cache", _create_season_metadata_cache_table),
    Migration("015_resource_search_jobs", _create_resource_search_jobs_table),
    Migration("031_workflow_schema_compatibility", _upgrade_legacy_workflow_schema),
    Migration("032_agent_tokens", _create_agent_tokens_table),
    Migration("033_webhook_outbox", _create_webhook_tables),
    Migration("034_pwa_devices", _create_pwa_device_table),
    Migration("035_library_media_identities", _create_library_media_identity_table),
    Migration("036_library_inventory_ledger", _create_library_inventory_ledger_tables),
    Migration("037_audit_records_schema_compatibility", _upgrade_audit_records_schema),
    Migration("038_audit_records_legacy_rebuild", _rebuild_legacy_audit_records),
    Migration("039_subscription_resource_observations", _create_subscription_resource_observations_table),
    Migration("040_strm_manifest_entries", _create_strm_manifest_table),
    Migration("041_organization_settings", _add_organization_settings_column),
    Migration("042_directory_dirty_leases", _upgrade_directory_dirty_outbox),
    Migration("043_p115_login_devices", _create_p115_login_devices_table),
    Migration(
        "044_workflow_schema_compatibility_followup",
        _upgrade_workflow_schema_compatibility_followup,
    ),
    Migration("045_workflow_stage_sequence", _upgrade_workflow_stage_sequence),
    Migration("046_workflow_stage_created_at", _upgrade_workflow_stage_created_at),
    Migration(
        "047_directory_dirty_generations",
        _create_directory_dirty_generation_table,
    ),
    Migration(
        "048_organization_operation_workflow",
        _add_organization_operation_workflow,
    ),
    Migration("049_notification_quiet_hours", _add_notification_quiet_hours),
    Migration("050_strm_cleanup_plans", _create_strm_cleanup_plan_table),
    Migration("051_organization_cancel_requested", _add_organization_cancel_requested),
    Migration("052_task_target_directory", _add_task_target_directory),
    Migration("053_organization_history", _create_organization_history_table),
    Migration("054_strm_operations", _create_strm_operations_table),
    Migration("055_strm_cleanup_idempotency", _add_strm_cleanup_idempotency),
    Migration("056_strm_operation_leases", _add_strm_operation_leases),
    Migration("057_empty_directory_cleanup_plans", _create_empty_directory_cleanup_plan_table),
    Migration("058_prowlarr_application_settings_columns", _add_prowlarr_application_settings_columns),
    Migration("059_library_scan_lifecycle", _upgrade_library_scan_lifecycle),
    Migration("060_workflow_evidence", _create_workflow_evidence_table),
    Migration("061_library_scan_mode_evidence", _invalidate_legacy_library_scan_modes),
    Migration("062_task_lease_fencing", _add_task_lease_token),
    Migration("063_library_scan_pickcode", _add_library_scan_pickcode),
    Migration(
        "064_managed_directory_ownership",
        _create_managed_directory_ownership_table,
    ),
    Migration("065_subscription_episode_columns", _add_subscription_episode_columns),
    Migration("066_quality_profile_scope_columns", _add_quality_profile_scope_columns),
    Migration(
        "067_subscription_observation_seen_count",
        _add_subscription_observation_seen_count,
    ),
    Migration("068_repair_subscription_legacy_mode", _repair_subscription_legacy_mode),
    Migration(
        "069_repair_subscription_null_match_count",
        _repair_subscription_null_match_count,
    ),
    Migration(
        "070_subscription_partial_unique_indexes",
        _add_subscription_partial_unique_indexes,
    ),
    Migration(
        "071_organization_operation_partial_unique_plan",
        _add_organization_operation_partial_unique_index,
    ),
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
