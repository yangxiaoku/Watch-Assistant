#!/usr/bin/env python3
"""Start an extracted release with a legacy SQLite schema and check health."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_manifest import (
    validate_runtime_manifest_file,
    validate_version_commit_file,
)

_REQUIRED_FILES = (
    "VERSION",
    "release-manifest.json",
    "frontend/dist/index.html",
    "src/watch_assistant/app.py",
    "src/watch_assistant/release_metadata.py",
    "scripts/release_startup_smoke.py",
    "scripts/release_manifest.py",
    "scripts/systemd_release_update.py",
    "scripts/systemd_release_prepare.py",
    "scripts/postdeploy_release_check.py",
    "scripts/deploy_systemd_release.sh",
)
_LEGACY_APPLICATION_SETTINGS = """
CREATE TABLE application_settings (
    id VARCHAR(16) PRIMARY KEY,
    logging_level VARCHAR(16) NOT NULL,
    retention_days INTEGER NOT NULL,
    max_file_mb INTEGER NOT NULL,
    inspection_auto_start_enabled BOOLEAN NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL,
    content_policy_json TEXT NOT NULL DEFAULT '{}',
    organization_settings_json TEXT NOT NULL DEFAULT '{}',
    managed_tmdb_key_encrypted TEXT,
    managed_tmdb_updated_at DATETIME,
    managed_p115_cookie_encrypted TEXT,
    managed_p115_updated_at DATETIME,
    updated_at DATETIME NOT NULL
);
CREATE TABLE schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _version_commit(release_root: Path) -> str:
    return validate_version_commit_file(release_root / "VERSION")


def _validate_manifest(release_root: Path, release: str) -> None:
    validate_runtime_manifest_file(release_root / "release-manifest.json", release)


def _create_legacy_database(database_path: Path, migration_ids: list[str]) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_LEGACY_APPLICATION_SETTINGS)
        connection.executemany(
            "INSERT INTO schema_migrations (migration_id) VALUES (?)",
            ((migration_id,) for migration_id in migration_ids),
        )
        connection.commit()
    finally:
        connection.close()


def _configure_environment(
    release_root: Path, database_path: Path, state_directory: Path
) -> dict[str, str | None]:
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "release-smoke",
        "WEB_PASSWORD_HASH": "release-smoke",
        "SCRIPT_TOKEN_HASH": "release-smoke",
        "PANSOU_BASE_URL": "http://127.0.0.1:1",
        "FRONTEND_DIST_DIR": str(release_root / "frontend/dist"),
        "STATE_DIRECTORY": str(state_directory),
        "CACHE_WARM_ENABLED": "false",
        "SUBSCRIPTION_SCHEDULER_ENABLED": "false",
        "PROWLARR_ENABLED": "false",
        "INSPECTION_ENABLED": "false",
        "P115_ENABLED": "false",
        "ORGANIZATION_PLAN_ENABLED": "false",
        "ORGANIZATION_EXECUTION_ENABLED": "false",
        "ORGANIZATION_WRITE_ENABLED": "false",
        "PERMANENT_DELETE_ENABLED": "false",
        "STRM_FULL_ENABLED": "false",
        "STRM_INCREMENTAL_ENABLED": "false",
        "STRM_CLEANUP_ENABLED": "false",
        "STRM_PLAYBACK_ENABLED": "false",
        "P115_COOKIE_PATH": str(state_directory / "missing-cookie"),
        "STRM_OUTPUT_ROOT": str(state_directory / "strm"),
    }
    previous = {
        name: os.environ.get(name)
        for name in (*values, "WATCH_ASSISTANT_RELEASE")
    }
    os.environ.update(values)
    os.environ.pop("WATCH_ASSISTANT_RELEASE", None)
    return previous


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


async def _start_and_check_health(release_root: Path, release: str) -> None:
    from watch_assistant.app import create_app

    with tempfile.TemporaryDirectory(prefix="watch-assistant-release-smoke-") as temp_dir:
        state_directory = Path(temp_dir)
        database_path = state_directory / "legacy.db"
        from watch_assistant.migrations import MIGRATIONS

        migration_ids = [
            migration.id
            for migration in MIGRATIONS
            if migration.id != "058_prowlarr_application_settings_columns"
        ]
        _create_legacy_database(database_path, migration_ids)
        previous = _configure_environment(
            release_root, database_path, state_directory
        )
        try:
            app = create_app(
                frontend_dir=release_root / "frontend/dist",
                release_root=release_root,
            )
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://release-smoke",
            ) as client:
                response = await client.get("/api/v1/health")
            payload = response.json()
            if (
                response.status_code != 200
                or payload.get("status") != "ok"
                or payload.get("release") != release
            ):
                raise RuntimeError("health_check_failed")
        finally:
            _restore_environment(previous)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args()
    release_root = args.release_root.resolve()
    missing = [name for name in _REQUIRED_FILES if not (release_root / name).is_file()]
    if missing:
        print("RELEASE_SMOKE_RESULT=failed")
        print("RELEASE_SMOKE_CODE=missing_release_files")
        print("RELEASE_SMOKE_MISSING_COUNT=" + str(len(missing)))
        return 1

    try:
        release = _version_commit(release_root)
        _validate_manifest(release_root, release)
        sys.path.insert(0, str(release_root / "src"))
        asyncio.run(_start_and_check_health(release_root, release))
    except Exception as exc:  # noqa: BLE001 - report only a stable diagnostic
        print("RELEASE_SMOKE_RESULT=failed")
        print(
            "RELEASE_SMOKE_EXCEPTION="
            + type(exc).__module__
            + "."
            + type(exc).__name__
        )
        return 1

    print("RELEASE_SMOKE_RESULT=ok")
    print("RELEASE_SMOKE_HEALTH=ok")
    print("RELEASE_SMOKE_LEGACY_MIGRATION=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
