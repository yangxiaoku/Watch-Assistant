"""FastAPI application factory."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.api.auth import router as auth_router
from watch_assistant.api.search import router as search_router
from watch_assistant.api.tasks import router as tasks_router
from watch_assistant.config import Settings, load_tgto_contract
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import Database, create_database, initialize_database
from watch_assistant.security import SecurityManager
from watch_assistant.services.search import SearchService
from watch_assistant.services.tasks import TaskService


def create_app(
    *,
    database: Database | None = None,
    crypto: SecretCrypto | None = None,
    tmdb_client: TmdbClient | None = None,
    pansou_client: PanSouClient | None = None,
    security_manager: SecurityManager | None = None,
    share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    push_supported: bool | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owned: list[object] = []
        if not hasattr(application.state, "search_service"):
            settings = Settings(_secrets_dir="/run/secrets")
            runtime_database = database or create_database(settings.database_url)
            runtime_crypto = crypto or SecretCrypto(
                settings.encryption_key.get_secret_value()
            )
            runtime_tmdb = tmdb_client or TmdbClient(
                settings.tmdb_api_key.get_secret_value()
            )
            runtime_pansou = pansou_client or PanSouClient(settings.pansou_base_url)
            await initialize_database(runtime_database.engine)
            application.state.search_service = SearchService(
                runtime_database.session_factory,
                tmdb_client=runtime_tmdb,
                pansou_client=runtime_pansou,
                crypto=runtime_crypto,
                share_domains=share_domains,
            )
            application.state.task_service = TaskService(runtime_database.session_factory)
            application.state.security_manager = security_manager or SecurityManager(
                web_password_hash=settings.web_password_hash.get_secret_value(),
                script_token_hash=settings.script_token_hash.get_secret_value(),
                cookie_secure=settings.cookie_secure,
            )
            contract = load_tgto_contract(settings.tgto_contract_path)
            application.state.push_supported = contract.get("supported") is True
            application.state.database = runtime_database
            owned = [runtime_database, runtime_tmdb, runtime_pansou]
        try:
            yield
        finally:
            for resource in owned:
                if isinstance(resource, Database):
                    await resource.engine.dispose()
                elif hasattr(resource, "aclose"):
                    await resource.aclose()

    application = FastAPI(title="Watch Assistant", lifespan=lifespan)
    if database and crypto and tmdb_client and pansou_client:
        application.state.database = database
        application.state.search_service = SearchService(
            database.session_factory,
            tmdb_client=tmdb_client,
            pansou_client=pansou_client,
            crypto=crypto,
            share_domains=share_domains,
        )
        application.state.task_service = TaskService(database.session_factory)
        if security_manager is not None:
            application.state.security_manager = security_manager
        application.state.push_supported = (
            True if push_supported is None else push_supported
        )

    @application.get("/api/v1/health")
    async def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "push_supported": getattr(
                application.state, "push_supported", False
            ),
        }

    application.include_router(search_router)
    application.include_router(tasks_router)
    application.include_router(auth_router)
    static_path = frontend_dir or Path(
        os.environ.get("FRONTEND_DIST_DIR", "frontend/dist")
    )
    if static_path.is_dir():
        application.mount("/", StaticFiles(directory=static_path, html=True), name="frontend")
    return application


app = create_app()
