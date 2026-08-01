from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from watch_assistant.app import create_app
from watch_assistant.db import create_database
from watch_assistant.migrations import MIGRATIONS


@pytest.mark.integration
async def test_movie_deep_link_serves_spa_without_masking_missing_assets(
    tmp_path: Path,
):
    frontend_dir = tmp_path / "dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text(
        "<h1>Watch Assistant</h1>", encoding="utf-8"
    )
    app = create_app(frontend_dir=frontend_dir)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        deep_link = await client.get("/movie/27205")
        browse_link = await client.get("/favorites")
        settings_link = await client.get("/settings")
        library_link = await client.get("/library")
        workflows_link = await client.get("/workflows")
        notifications_link = await client.get("/notifications")
        missing_asset = await client.get("/assets/missing.js")

    assert deep_link.status_code == 200
    assert "Watch Assistant" in deep_link.text
    assert browse_link.status_code == 200
    assert "Watch Assistant" in browse_link.text
    assert settings_link.status_code == 200
    assert "Watch Assistant" in settings_link.text
    assert library_link.status_code == 200
    assert workflows_link.status_code == 200
    assert notifications_link.status_code == 200
    assert missing_asset.status_code == 404


@pytest.mark.integration
async def test_app_startup_upgrades_legacy_prowlarr_settings_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database_path = tmp_path / "legacy.db"
    database = create_database(f"sqlite+aiosqlite:///{database_path}")
    legacy_migration_ids = [
        migration.id
        for migration in MIGRATIONS
        if migration.id != "058_prowlarr_application_settings_columns"
    ]
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            """
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
            )
            """
        )
        await connection.exec_driver_sql(
            """
            CREATE TABLE schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await connection.execute(
            text(
                "INSERT INTO schema_migrations (migration_id) VALUES (:migration_id)"
            ),
            [{"migration_id": migration_id} for migration_id in legacy_migration_ids],
        )
    await database.engine.dispose()

    contract_path = tmp_path / "tgto-contract.json"
    contract_path.write_text('{"supported": false}', encoding="utf-8")
    settings = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TGTO_BASE_URL": "http://tgto.test",
        "TGTO_CONTRACT_PATH": str(contract_path),
        "CACHE_WARM_ENABLED": "false",
        "SUBSCRIPTION_SCHEDULER_ENABLED": "false",
        "P115_ENABLED": "false",
        "ORGANIZATION_PLAN_ENABLED": "false",
        "ORGANIZATION_EXECUTION_ENABLED": "false",
        "ORGANIZATION_WRITE_ENABLED": "false",
        "PERMANENT_DELETE_ENABLED": "false",
        "STRM_FULL_ENABLED": "false",
        "STRM_INCREMENTAL_ENABLED": "false",
        "STRM_CLEANUP_ENABLED": "false",
        "STRM_PLAYBACK_ENABLED": "false",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    app = create_app(frontend_dir=tmp_path / "missing")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.integration
async def test_supported_contract_fails_closed_without_production_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract_path = tmp_path / "tgto-contract.json"
    contract_path.write_text('{"supported": true}', encoding="utf-8")
    settings = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TGTO_BASE_URL": "http://tgto.test",
        "TGTO_CONTRACT_PATH": str(contract_path),
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    app = create_app(frontend_dir=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="no production worker"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.integration
async def test_app_passes_inspection_settings_to_qbittorrent_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract_path = tmp_path / "tgto-contract.json"
    contract_path.write_text('{"supported": false}', encoding="utf-8")
    settings = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
        "TGTO_BASE_URL": "http://tgto.test",
        "TGTO_CONTRACT_PATH": str(contract_path),
        "CACHE_WARM_ENABLED": "false",
        "INSPECTION_ENABLED": "true",
        "QBITTORRENT_BASE_URL": "http://qbittorrent.test",
        "QBITTORRENT_USERNAME": "user",
        "QBITTORRENT_PASSWORD": "password",
        "INSPECTION_CONCURRENCY": "9",
        "INSPECTION_ITEM_TIMEOUT_SECONDS": "45",
        "INSPECTION_POLL_INTERVAL_SECONDS": "0.5",
        "INSPECTION_REQUEST_TIMEOUT_SECONDS": "12",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    captured: dict[str, object] = {}

    class CapturingQbittorrent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("watch_assistant.app.QbittorrentClient", CapturingQbittorrent)
    app = create_app(frontend_dir=tmp_path / "missing")

    async with app.router.lifespan_context(app):
        assert captured["args"] == ("http://qbittorrent.test", "user", "password")
        assert captured["kwargs"] == {
            "concurrency": 9,
            "item_timeout": 45.0,
            "poll_interval": 0.5,
            "request_timeout": 12.0,
        }
