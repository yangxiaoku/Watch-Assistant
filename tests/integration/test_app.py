from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from tests.unit.factories import make_security_manager
from watch_assistant.app import create_app
from watch_assistant.db import create_database
from watch_assistant.migrations import MIGRATIONS
from watch_assistant.services.p115_checkin_scheduler import P115CheckInScheduler


@pytest.mark.integration
async def test_movie_deep_link_serves_spa_without_masking_missing_assets(
    tmp_path: Path,
):
    frontend_dir = tmp_path / "dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text(
        "<h1>Watch Assistant</h1>", encoding="utf-8"
    )
    app = create_app(frontend_dir=frontend_dir, security_manager=make_security_manager())

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
                p115_checkin_settings_json TEXT NOT NULL DEFAULT '{}',
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

    settings = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
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

    app = create_app(frontend_dir=tmp_path / "missing", security_manager=make_security_manager())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.integration
async def test_app_passes_inspection_settings_to_qbittorrent_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "tmdb-key",
        "WEB_PASSWORD_HASH": "web-hash",
        "SCRIPT_TOKEN_HASH": "script-hash",
        "PANSOU_BASE_URL": "http://pansou.test",
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
    app = create_app(frontend_dir=tmp_path / "missing", security_manager=make_security_manager())

    async with app.router.lifespan_context(app):
        assert captured["args"] == ("http://qbittorrent.test", "user", "password")
        assert captured["kwargs"] == {
            "concurrency": 9,
            "item_timeout": 45.0,
            "poll_interval": 0.5,
            "request_timeout": 12.0,
        }


class _ReadyAdapter:
    """P115Adapter stand-in that reports ready immediately at startup."""

    def __init__(self, cookie_provider, target_cid, *, max_concurrency):
        self.cookie_provider = cookie_provider
        self.target_cid = target_cid
        self.max_concurrency = max_concurrency

    async def ensure_available(self) -> bool:
        return True

    async def validate_cookie(self, _cookie: str) -> None:
        return None

    async def submit_magnet(self, _url: str):
        raise AssertionError("magnet submission must not run")

    async def save_share(self, _url: str, _password: str | None):
        raise AssertionError("share submission must not run")

    async def get_status_for_task(self, _remote_ref, *, target_directory_id):
        return None

    async def aclose(self) -> None:
        return None


class _NoopWorker:
    def __init__(self, *_args, **_kwargs):
        pass

    async def recover_expired(self) -> int:
        return 0

    async def run_forever(self, stop_event) -> None:
        await stop_event.wait()


def _set_checkin_app_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    check_in_enabled: str = "true",
) -> None:
    cookie_path = tmp_path / "p115-cookie"
    cookie_path.write_text("UID=u; CID=c; KID=k; SEID=s", encoding="ascii", newline="")
    values = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "TMDB_API_KEY": "env-key",
        "WEB_PASSWORD_HASH": "unused",
        "SCRIPT_TOKEN_HASH": "unused",
        "PANSOU_BASE_URL": "http://pansou.test",
        "CACHE_WARM_ENABLED": "false",
        "SUBSCRIPTION_SCHEDULER_ENABLED": "false",
        "LIBRARY_SCAN_SCHEDULER_ENABLED": "false",
        "ORGANIZATION_PLAN_ENABLED": "false",
        "ORGANIZATION_EXECUTION_ENABLED": "false",
        "ORGANIZATION_WRITE_ENABLED": "false",
        "STRM_FULL_ENABLED": "false",
        "STRM_INCREMENTAL_ENABLED": "false",
        "P115_ENABLED": "true",
        "P115_COOKIE_PATH": str(cookie_path),
        "P115_TARGET_CID": "1",
        "P115_MAX_CONCURRENCY": "1",
        "P115_CHECK_IN_ENABLED": check_in_enabled,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("watch_assistant.app.P115Adapter", _ReadyAdapter)
    monkeypatch.setattr("watch_assistant.app.TaskWorker", _NoopWorker)


@pytest.mark.integration
async def test_p115_checkin_scheduler_wired_when_p115_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_checkin_app_env(monkeypatch, tmp_path, check_in_enabled="true")

    app = create_app(
        frontend_dir=tmp_path / "missing", security_manager=make_security_manager()
    )
    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is True
        scheduler = getattr(app.state, "p115_checkin_scheduler", None)
        assert scheduler is not None
        assert isinstance(scheduler, P115CheckInScheduler)


@pytest.mark.integration
async def test_p115_checkin_scheduler_wired_when_ready_regardless_of_env_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_checkin_app_env(monkeypatch, tmp_path, check_in_enabled="false")

    app = create_app(
        frontend_dir=tmp_path / "missing", security_manager=make_security_manager()
    )
    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is True
        scheduler = getattr(app.state, "p115_checkin_scheduler", None)
        assert scheduler is not None
        assert isinstance(scheduler, P115CheckInScheduler)


@pytest.mark.integration
async def test_p115_checkin_scheduler_not_wired_when_p115_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_checkin_app_env(monkeypatch, tmp_path, check_in_enabled="true")

    class NotReadyAdapter(_ReadyAdapter):
        async def ensure_available(self) -> bool:
            return False

    monkeypatch.setattr("watch_assistant.app.P115Adapter", NotReadyAdapter)

    app = create_app(
        frontend_dir=tmp_path / "missing", security_manager=make_security_manager()
    )
    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is False
        assert not hasattr(app.state, "p115_checkin_scheduler")
