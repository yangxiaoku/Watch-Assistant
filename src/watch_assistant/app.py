"""FastAPI application factory."""

import asyncio
import os
import re
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.qbittorrent import QbittorrentClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.api.auth import router as auth_router
from watch_assistant.api.credentials import router as credentials_router
from watch_assistant.api.inspection import router as inspection_router
from watch_assistant.api.maintenance import router as maintenance_router
from watch_assistant.api.organization_plan import router as organization_plan_router
from watch_assistant.api.search import router as search_router
from watch_assistant.api.settings import router as settings_router
from watch_assistant.api.settings_p115 import router as p115_settings_router
from watch_assistant.api.tasks import router as tasks_router
from watch_assistant.config import Settings, load_tgto_contract
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import Database, create_database, initialize_database
from watch_assistant.security import SecurityManager
from watch_assistant.services.cache_warm import CacheWarmer
from watch_assistant.services.credentials import CredentialService
from watch_assistant.services.inspection import InspectionService, InspectionWorker
from watch_assistant.services.maintenance import MaintenanceService
from watch_assistant.services.organization_plan import OrganizationPlanService
from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
)
from watch_assistant.services.p115_settings import P115SettingsService
from watch_assistant.services.search import SearchService
from watch_assistant.services.settings import SettingsService
from watch_assistant.services.tasks import TaskService
from watch_assistant.worker import TaskAdapter, TaskWorker

_RELEASE_SHA = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
_TRUSTED_RELEASE_PATH = Path("/opt/watch-assistant/current")


def create_app(
    *,
    database: Database | None = None,
    crypto: SecretCrypto | None = None,
    tmdb_client: TmdbClient | None = None,
    pansou_client: PanSouClient | None = None,
    security_manager: SecurityManager | None = None,
    share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    push_supported: bool | None = None,
    qbittorrent_client: QbittorrentClient | None = None,
    task_adapter: TaskAdapter | None = None,
    frontend_dir: Path | None = None,
    organization_plan_enabled: bool | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owned: list[object] = []
        warm_stop: asyncio.Event | None = None
        warm_task: asyncio.Task[None] | None = None
        inspection_stop: asyncio.Event | None = None
        inspection_task: asyncio.Task[None] | None = None
        task_stop: asyncio.Event | None = None
        task_task: asyncio.Task[None] | None = None

        async def apply_p115_runtime(ready: bool) -> None:
            nonlocal task_stop, task_task
            application.state.p115_ready = ready
            application.state.push_capabilities = {
                "magnet": ready,
                "share": False,
            }
            adapter = getattr(application.state, "task_adapter", None)
            if adapter is None:
                return
            if not ready:
                if task_stop is not None:
                    task_stop.set()
                if task_task is not None:
                    task_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task_task
                task_stop = None
                task_task = None
                if hasattr(application.state, "task_worker"):
                    delattr(application.state, "task_worker")
                return
            if getattr(application.state, "task_worker", None) is not None:
                return
            worker = TaskWorker(
                runtime_database.session_factory,
                runtime_crypto,
                adapter,
                owner=_worker_owner(),
                event_logger=application.state.settings_service,
            )
            await worker.recover_expired()
            application.state.task_worker = worker
            task_stop = asyncio.Event()
            task_task = asyncio.create_task(
                worker.run_forever(task_stop), name="watch-assistant-task-worker"
            )

        existing_credentials = getattr(application.state, "credential_service", None)
        if existing_credentials is not None and not getattr(
            application.state, "_credentials_loaded", False
        ):
            managed_tmdb, _managed_cookie = await existing_credentials.load_managed()
            if managed_tmdb is not None:
                existing_tmdb = getattr(existing_credentials, "_tmdb_client", None)
                set_api_key = getattr(existing_tmdb, "set_api_key", None)
                if callable(set_api_key):
                    set_api_key(managed_tmdb)
            application.state._credentials_loaded = True

        if not hasattr(application.state, "search_service"):
            credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
            secrets_dir = Path(credentials_directory or "/run/secrets")
            settings = Settings(
                _secrets_dir=secrets_dir if secrets_dir.is_dir() else None
            )
            if organization_plan_enabled is None:
                application.state.organization_plan_enabled = (
                    settings.organization_plan_enabled
                )
            if settings.p115_enabled and settings.p115_target_cid is None:
                raise RuntimeError("P115_ENABLED requires P115_TARGET_CID")
            contract = load_tgto_contract(settings.tgto_contract_path)
            if contract.get("supported") is True:
                raise RuntimeError(
                    "TgtoDrive contract is marked supported, but no production worker "
                    "is configured"
                )
            runtime_database = database or create_database(settings.database_url)
            await initialize_database(runtime_database.engine)
            runtime_crypto = crypto or SecretCrypto(
                settings.encryption_key.get_secret_value()
            )
            runtime_pansou = pansou_client or PanSouClient(settings.pansou_base_url)
            application.state.settings_service = SettingsService(
                runtime_database.session_factory,
                state_directory=_state_directory(runtime_database),
            )
            fallback_cookie_provider = CookieProvider(settings.p115_cookie_path)
            composite_cookie_provider = CompositeCookieProvider(
                fallback_cookie_provider
            )
            credential_service = CredentialService(
                runtime_database.session_factory,
                runtime_crypto,
                environment_tmdb_key=settings.tmdb_api_key.get_secret_value(),
                fallback_cookie_provider=fallback_cookie_provider,
                cookie_provider=composite_cookie_provider,
                event_logger=application.state.settings_service,
                runtime_state=application.state,
                p115_runtime_callback=apply_p115_runtime,
            )
            managed_tmdb, _managed_cookie = await credential_service.load_managed()
            runtime_tmdb = tmdb_client or TmdbClient(
                managed_tmdb or settings.tmdb_api_key.get_secret_value(),
                base_url=settings.tmdb_base_url,
            )
            if tmdb_client is not None and managed_tmdb is not None:
                set_api_key = getattr(tmdb_client, "set_api_key", None)
                if callable(set_api_key):
                    set_api_key(managed_tmdb)
            application.state.credential_service = credential_service
            await application.state.settings_service.log_event(
                "application.startup", fields={"status": "started"}
            )
            application.state.search_service = SearchService(
                runtime_database.session_factory,
                tmdb_client=runtime_tmdb,
                pansou_client=runtime_pansou,
                crypto=runtime_crypto,
                share_domains=share_domains,
                pansou_max_concurrency=settings.pansou_max_concurrency,
                event_logger=application.state.settings_service,
            )
            application.state.task_service = TaskService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.maintenance_service = MaintenanceService(
                runtime_database.session_factory
            )
            if security_manager is None:
                application.state.security_manager = SecurityManager(
                    web_password_hash=settings.web_password_hash.get_secret_value(),
                    script_token_hash=settings.script_token_hash.get_secret_value(),
                    cookie_secure=settings.cookie_secure,
                    session_factory=runtime_database.session_factory,
                    session_ttl=timedelta(hours=settings.web_session_ttl_hours),
                )
            else:
                security_manager.configure_session_store(
                    runtime_database.session_factory,
                    session_ttl=timedelta(hours=settings.web_session_ttl_hours),
                )
                application.state.security_manager = security_manager
            application.state.push_supported = False
            application.state.push_capabilities = {
                "magnet": False,
                "share": False,
            }
            application.state.p115_ready = False
            application.state.database = runtime_database
            owned = [runtime_database, runtime_tmdb, runtime_pansou]
            cookie_provider = composite_cookie_provider
            runtime_task_adapter: TaskAdapter | None = None
            if settings.p115_enabled:
                runtime_task_adapter = task_adapter or P115Adapter(
                    cookie_provider,
                    settings.p115_target_cid,
                    max_concurrency=settings.p115_max_concurrency,
                )
                application.state.task_adapter = runtime_task_adapter
                owned.append(runtime_task_adapter)
                credential_service.bind_runtime(
                    tmdb_client=runtime_tmdb,
                    p115_adapter=runtime_task_adapter,
                    runtime_state=application.state,
                )
                p115_ready = await _ensure_adapter_available(runtime_task_adapter)
                application.state.p115_ready = p115_ready
                if p115_ready:
                    application.state.task_worker = TaskWorker(
                        runtime_database.session_factory,
                        runtime_crypto,
                        runtime_task_adapter,
                        owner=_worker_owner(),
                        event_logger=application.state.settings_service,
                    )
                    application.state.push_capabilities = {
                        "magnet": True,
                        "share": False,
                    }
            await application.state.settings_service.log_event(
                "p115.readiness",
                fields={
                    "status": "ready" if application.state.p115_ready else "unavailable"
                },
            )
            application.state.p115_settings_service = P115SettingsService(
                enabled=settings.p115_enabled,
                cookie_provider=cookie_provider,
                cookie_path=settings.p115_cookie_path,
                target_configured=settings.p115_target_cid is not None,
                max_concurrency=settings.p115_max_concurrency,
                adapter=runtime_task_adapter,
            )
            if runtime_task_adapter is None:
                credential_service.bind_runtime(
                    tmdb_client=runtime_tmdb,
                    p115_adapter=None,
                    runtime_state=application.state,
                )
            inspection_client = qbittorrent_client
            if inspection_client is None and settings.inspection_configured:
                inspection_client = QbittorrentClient(
                    settings.qbittorrent_base_url,
                    settings.qbittorrent_username.get_secret_value(),
                    settings.qbittorrent_password.get_secret_value(),
                    concurrency=settings.inspection_concurrency,
                    item_timeout=settings.inspection_item_timeout_seconds,
                    poll_interval=settings.inspection_poll_interval_seconds,
                    request_timeout=settings.inspection_request_timeout_seconds,
                )
                owned.append(inspection_client)
            application.state.inspection_supported = inspection_client is not None
            if inspection_client is not None:
                application.state.inspection_client = inspection_client
                application.state.inspection_service = InspectionService(
                    runtime_database.session_factory,
                    event_logger=application.state.settings_service,
                )
                application.state.inspection_worker = InspectionWorker(
                    runtime_database.session_factory,
                    runtime_crypto,
                    inspection_client,
                    event_logger=application.state.settings_service,
                )
            if settings.cache_warm_enabled:
                warmer = CacheWarmer(
                    application.state.search_service,
                    runtime_database.session_factory,
                    timezone_name=settings.cache_warm_timezone,
                    concurrency=settings.cache_warm_concurrency,
                    event_logger=application.state.settings_service,
                )
                warm_stop = asyncio.Event()
                warm_task = asyncio.create_task(
                    warmer.run_forever(warm_stop),
                    name="watch-assistant-cache-warmer",
                )
                application.state.cache_warmer = warmer
        task_adapter_resource = getattr(application.state, "task_adapter", None)
        if task_adapter_resource is not None and not hasattr(
            application.state, "p115_ready"
        ):
            p115_ready = await _ensure_adapter_available(task_adapter_resource)
            application.state.p115_ready = p115_ready
            if not p115_ready:
                application.state.push_capabilities = {
                    "magnet": False,
                    "share": False,
                }
                application.state.task_worker = None
        worker = getattr(application.state, "inspection_worker", None)
        if worker is not None:
            inspection_stop = asyncio.Event()
            inspection_task = asyncio.create_task(
                worker.run_forever(inspection_stop),
                name="watch-assistant-inspection-worker",
            )
        task_worker = getattr(application.state, "task_worker", None)
        if task_worker is not None:
            await task_worker.recover_expired()
            task_stop = asyncio.Event()
            task_task = asyncio.create_task(
                task_worker.run_forever(task_stop),
                name="watch-assistant-task-worker",
            )
        settings_service = getattr(application.state, "settings_service", None)
        if settings_service is not None:
            await settings_service.log_event(
                "application.readiness", fields={"status": "ready"}
            )
        try:
            yield
        finally:
            if task_task is not None and task_stop is not None:
                task_stop.set()
                task_task.cancel()
                with suppress(asyncio.CancelledError):
                    await task_task
            if inspection_task is not None and inspection_stop is not None:
                inspection_stop.set()
                inspection_task.cancel()
                with suppress(asyncio.CancelledError):
                    await inspection_task
            if warm_task is not None and warm_stop is not None:
                warm_stop.set()
                warm_task.cancel()
                with suppress(asyncio.CancelledError):
                    await warm_task
            inspection_client = getattr(application.state, "inspection_client", None)
            if (
                inspection_client is not None
                and inspection_client not in owned
                and hasattr(inspection_client, "aclose")
            ):
                await inspection_client.aclose()
            task_adapter_resource = getattr(application.state, "task_adapter", None)
            if (
                task_adapter_resource is not None
                and task_adapter_resource not in owned
                and hasattr(task_adapter_resource, "aclose")
            ):
                await task_adapter_resource.aclose()
            for resource in owned:
                if isinstance(resource, Database):
                    await resource.engine.dispose()
                elif hasattr(resource, "aclose"):
                    await resource.aclose()

    application = FastAPI(title="Watch Assistant", lifespan=lifespan)
    application.state.release = _resolve_release()
    application.state.started_at = time.monotonic()
    application.state.organization_plan_enabled = (
        organization_plan_enabled
        if organization_plan_enabled is not None
        else _env_flag("ORGANIZATION_PLAN_ENABLED")
    )
    if database and crypto and tmdb_client and pansou_client:
        application.state.database = database
        application.state.settings_service = SettingsService(
            database.session_factory,
            state_directory=_state_directory(database),
        )
        application.state.search_service = SearchService(
            database.session_factory,
            tmdb_client=tmdb_client,
            pansou_client=pansou_client,
            crypto=crypto,
            share_domains=share_domains,
            event_logger=application.state.settings_service,
        )
        application.state.task_service = TaskService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.maintenance_service = MaintenanceService(
            database.session_factory
        )
        application.state.organization_plan_service = OrganizationPlanService(
            database.session_factory
        )
        fallback_cookie_provider = CookieProvider(
            os.environ.get("P115_COOKIE_PATH", "/run/secrets/p115_cookie")
        )
        composite_cookie_provider = CompositeCookieProvider(fallback_cookie_provider)
        application.state.credential_service = CredentialService(
            database.session_factory,
            crypto,
            environment_tmdb_key=os.environ.get("TMDB_API_KEY", ""),
            fallback_cookie_provider=fallback_cookie_provider,
            cookie_provider=composite_cookie_provider,
            event_logger=application.state.settings_service,
            tmdb_client=tmdb_client,
            p115_adapter=task_adapter,
            runtime_state=application.state,
        )
        application.state.inspection_supported = qbittorrent_client is not None
        if qbittorrent_client is not None:
            application.state.inspection_client = qbittorrent_client
            application.state.inspection_service = InspectionService(
                database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.inspection_worker = InspectionWorker(
                database.session_factory,
                crypto,
                qbittorrent_client,
                event_logger=application.state.settings_service,
            )
        if security_manager is not None:
            security_manager.configure_session_store(database.session_factory)
            application.state.security_manager = security_manager
        application.state.push_supported = False
        application.state.push_capabilities = {
            "magnet": task_adapter is not None,
            "share": False,
        }
        if task_adapter is not None:
            application.state.task_adapter = task_adapter
            application.state.task_worker = TaskWorker(
                database.session_factory,
                crypto,
                task_adapter,
                owner=_worker_owner(),
                event_logger=application.state.settings_service,
            )

    @application.get("/api/v1/health")
    async def health() -> dict[str, object]:
        capabilities = getattr(
            application.state,
            "push_capabilities",
            {"magnet": False, "share": False},
        )
        inspection_auto_start_enabled = False
        settings_service = getattr(application.state, "settings_service", None)
        if settings_service is not None:
            try:
                inspection_auto_start_enabled = (
                    await settings_service.get_inspection()
                ).auto_start_enabled
            except Exception:  # noqa: BLE001 - health remains available
                inspection_auto_start_enabled = False
        return {
            "status": "ok",
            "push_supported": getattr(application.state, "push_supported", False),
            "push_capabilities": capabilities,
            "inspection_supported": getattr(
                application.state, "inspection_supported", False
            ),
            "inspection_auto_start_enabled": inspection_auto_start_enabled,
            "organization_plan_enabled": bool(
                getattr(application.state, "organization_plan_enabled", False)
            ),
        }

    application.include_router(search_router)
    application.include_router(settings_router)
    application.include_router(credentials_router)
    application.include_router(p115_settings_router)
    application.include_router(tasks_router)
    application.include_router(auth_router)
    application.include_router(maintenance_router)
    application.include_router(inspection_router)
    application.include_router(organization_plan_router)
    static_path = frontend_dir or Path(
        os.environ.get("FRONTEND_DIST_DIR", "frontend/dist")
    )
    if static_path.is_dir():
        index_path = static_path / "index.html"

        @application.get("/movie/{frontend_path:path}", include_in_schema=False)
        @application.get("/tv/{frontend_path:path}", include_in_schema=False)
        async def frontend_movie_route(frontend_path: str) -> FileResponse:
            return FileResponse(index_path)

        @application.get("/movies", include_in_schema=False)
        @application.get("/tv", include_in_schema=False)
        @application.get("/popular", include_in_schema=False)
        @application.get("/favorites", include_in_schema=False)
        @application.get("/history", include_in_schema=False)
        @application.get("/search", include_in_schema=False)
        @application.get("/settings", include_in_schema=False)
        @application.get("/organization-plans", include_in_schema=False)
        async def frontend_browse_route() -> FileResponse:
            return FileResponse(index_path)

        application.mount(
            "/", StaticFiles(directory=static_path, html=True), name="frontend"
        )
    return application


def _resolve_release(trusted_path: Path | None = None) -> str:
    configured = os.environ.get("WATCH_ASSISTANT_RELEASE")
    if configured:
        return configured
    try:
        release_name = (trusted_path or _TRUSTED_RELEASE_PATH).resolve(strict=True).name
    except (OSError, RuntimeError, ValueError):
        return "unknown"
    if _RELEASE_SHA.fullmatch(release_name) is not None:
        return release_name.lower()
    return "unknown"


def _worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _state_directory(database: Database) -> Path:
    configured = os.environ.get("STATE_DIRECTORY")
    if configured:
        return Path(configured)
    database_path = database.engine.url.database
    if database_path and database_path != ":memory:":
        return Path(database_path).parent
    return Path.cwd() / ".watch-assistant-state"


async def _ensure_adapter_available(adapter: TaskAdapter) -> bool:
    ensure_available = getattr(adapter, "ensure_available", None)
    if ensure_available is None:
        return True
    try:
        return bool(await ensure_available())
    except Exception:  # noqa: BLE001 - readiness must not expose adapter details
        return False


app = create_app()
