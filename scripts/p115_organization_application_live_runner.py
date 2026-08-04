"""Run the application organization planner against a managed live fixture.

This runner exercises the real settings, read-only scan, target catalog, and
plan-generation services with a temporary application database.  It never
enables the organization worker and never sends a remote write request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import tempfile
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import func, select

from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyDirectoryGateway
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import OrganizationPlan
from watch_assistant.models import OrganizationOperation
from watch_assistant.security import SecurityManager
from watch_assistant.services.organization_automation import (
    OrganizationAutomationService,
)
from watch_assistant.services.p115_credentials import CookieProvider
from watch_assistant.services.settings import (
    DEFAULT_ORGANIZATION_VIDEO_EXTENSIONS,
    OrganizationSettingsValidationError,
)

LIVE_ENV = "WATCH_ASSISTANT_P115_ORGANIZATION_APPLICATION_LIVE"


class _FakeTmdbClient:
    async def search_candidates(self, _query):
        return []


class _FakePanSouClient:
    async def aclose(self):
        return None


class _RecordingEvents:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def log_event(self, event: str, *, fields=None, counts=None, **_kwargs):
        self.events.append(
            {
                "event": event,
                "status": (fields or {}).get("status"),
                "error_code": (fields or {}).get("error_code"),
                "count": (counts or {}).get("count"),
            }
        )


def run_acceptance(
    *,
    source_id: str,
    target_id: str,
    cookie_path: Path,
    managed_scope_path: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if environment.get(LIVE_ENV) != "1":
        return _blocked("live_gate_closed")
    if not _stable_id(source_id) or not _stable_id(target_id) or source_id == target_id:
        return _blocked("invalid_scope")
    if not _scope_contains(managed_scope_path, source_id) or not _scope_contains(
        managed_scope_path, target_id
    ):
        return _blocked("scope_unverified")
    return asyncio.run(
        _run(
            source_id=source_id,
            target_id=target_id,
            cookie_path=cookie_path,
        )
    )


async def _run(
    *,
    source_id: str,
    target_id: str,
    cookie_path: Path,
) -> dict[str, object]:
    temporary = tempfile.TemporaryDirectory(prefix="wa-organization-app-live-")
    database = None
    client: httpx.AsyncClient | None = None
    try:
        base = Path(temporary.name)
        database = create_database(f"sqlite+aiosqlite:///{base / 'acceptance.db'}")
        await initialize_database(database.engine)
        password = secrets.token_urlsafe(24)
        password_hash = PasswordHash.recommended()
        app = create_app(
            database=database,
            crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
            tmdb_client=_FakeTmdbClient(),
            pansou_client=_FakePanSouClient(),
            security_manager=SecurityManager(
                web_password_hash=password_hash.hash(password),
                script_token_hash=password_hash.hash(secrets.token_urlsafe(24)),
            ),
            frontend_dir=base / "missing-frontend",
            organization_plan_enabled=True,
            organization_execution_enabled=False,
            organization_write_enabled=False,
            organization_write_contract_verified=False,
            strm_full_enabled=False,
            strm_incremental_enabled=False,
            strm_cleanup_enabled=False,
        )
        app.state.organization_cookie_provider = CookieProvider(cookie_path)
        _configure_preview_scope(app, source_id=source_id, target_id=target_id)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        )
        headers = await _login(client, password)
        current = await client.get("/api/v1/settings/organization", headers=headers)
        _expect(current, "settings_read_failed")
        revision = current.json()["revision"]
        configured = await client.patch(
            "/api/v1/settings/organization",
            headers=headers,
            json={
                "schedule_enabled": False,
                "source_directory_ids": [source_id],
                "target_directory_id": target_id,
                "video_extensions": _default_video_extensions(),
                "rename_enabled": True,
                "revision": revision,
            },
        )
        _expect(configured, "settings_write_failed")

        provider = app.state.organization_cookie_provider

        def gateway_factory(directory_ids):
            return P115ReadOnlyDirectoryGateway(
                provider,
                authorized_directory_ids=tuple(directory_ids),
                request_timeout_seconds=30,
            )

        events = _RecordingEvents()
        automation = OrganizationAutomationService(
            database.session_factory,
            app.state.settings_service,
            app.state.organization_preview_service,
            app.state.organization_plan_service,
            gateway_factory,
            auto_execute=False,
            event_logger=events,
        )
        attempted = await automation.run_once()
        result = automation.last_result
        async with database.session_factory() as session:
            plan_count = await session.scalar(select(func.count()).select_from(OrganizationPlan))
            operation_count = await session.scalar(
                select(func.count()).select_from(OrganizationOperation)
            )
            statuses = [
                value.value if hasattr(value, "value") else str(value)
                for value in (await session.scalars(select(OrganizationPlan.status))).all()
            ]
        if result is None:
            return _blocked("automation_result_missing")
        return {
            "status": "success",
            "settings": {
                "source_count": len(configured.json()["source_directory_ids"]),
                "target_configured": configured.json()["target_directory_id"] is not None,
                "schedule_enabled": configured.json()["schedule_enabled"],
            },
            "automation": {
                "attempted": attempted,
                "source_count": result.source_count,
                "scanned_count": result.scanned_count,
                "plan_count": result.plan_count,
                "queued_count": result.queued_count,
                "blocked_count": result.blocked_count,
            },
            "database": {
                "plan_count": int(plan_count or 0),
                "operation_count": int(operation_count or 0),
                "plan_statuses": statuses,
            },
            "events": events.events,
            "write_started": False,
            "remote_write_calls": 0,
            "output_database_is_temporary": True,
        }
    except OrganizationSettingsValidationError:
        return _blocked("settings_validation_failed")
    except Exception:  # noqa: BLE001 - provider and app details stay private
        return _blocked("application_acceptance_failed")
    finally:
        if client is not None:
            await client.aclose()
        if database is not None:
            await database.engine.dispose()
        temporary.cleanup()


async def _login(client: httpx.AsyncClient, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"password": password})
    _expect(response, "login_failed")
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def _expect(response: httpx.Response, code: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(code)


def _scope_contains(path: Path, directory_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    values = value.get("parent_ids") if isinstance(value, Mapping) else value
    return isinstance(values, Collection) and not isinstance(values, (str, bytes)) and directory_id in {
        str(item) for item in values
    }


def _configure_preview_scope(app, *, source_id: str, target_id: str) -> None:
    """Mirror the explicitly supplied managed IDs in the temporary app state."""
    app.state.organization_target_root_id = target_id
    app.state.p115_browsed_directory_ids = {source_id, target_id}


def _default_video_extensions() -> list[str]:
    """Keep live preview input aligned with the application's media defaults."""

    return list(DEFAULT_ORGANIZATION_VIDEO_EXTENSIONS)


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit() and not value.startswith("0")


def _blocked(error_code: str) -> dict[str, object]:
    return {"status": "blocked", "error_code": error_code, "write_started": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gated application organization planner")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--managed-scope-path", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    if args.live:
        environment[LIVE_ENV] = "1"
    else:
        environment.pop(LIVE_ENV, None)
    report = run_acceptance(
        source_id=args.source_id,
        target_id=args.target_id,
        cookie_path=args.cookie_path,
        managed_scope_path=args.managed_scope_path,
        environment=environment,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
