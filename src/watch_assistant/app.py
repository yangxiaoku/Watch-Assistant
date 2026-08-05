"""FastAPI application factory."""

import asyncio
import ipaddress
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.adapters.p115_c03_live_transport import p115_c03_timeout_executor
from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyDirectoryGateway
from watch_assistant.adapters.p115_library_write_contract import (
    P115OrganizationContract,
)
from watch_assistant.adapters.p115_playback_contract import P115PlaybackGateway
from watch_assistant.adapters.p115_playback_gateway import P115LivePlaybackGateway
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.prowlarr import ProwlarrClient
from watch_assistant.adapters.qbittorrent import QbittorrentClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.api.agent import router as agent_router
from watch_assistant.api.audit import router as audit_router
from watch_assistant.api.auth import router as auth_router
from watch_assistant.api.backups import router as backups_router
from watch_assistant.api.credentials import router as credentials_router
from watch_assistant.api.deployment import router as deployment_router
from watch_assistant.api.inspection import router as inspection_router
from watch_assistant.api.library import router as library_router
from watch_assistant.api.maintenance import router as maintenance_router
from watch_assistant.api.manual_import import router as manual_import_router
from watch_assistant.api.mcp import router as mcp_router
from watch_assistant.api.notifications import router as notifications_router
from watch_assistant.api.organization_history import (
    router as organization_history_router,
)
from watch_assistant.api.organization_operation import (
    router as organization_operation_router,
)
from watch_assistant.api.organization_plan import router as organization_plan_router
from watch_assistant.api.pwa import router as pwa_router
from watch_assistant.api.quality_profiles import router as quality_profiles_router
from watch_assistant.api.search import router as search_router
from watch_assistant.api.search_sources import router as search_sources_router
from watch_assistant.api.seasons import router as seasons_router
from watch_assistant.api.settings import router as settings_router
from watch_assistant.api.settings_p115 import router as p115_settings_router
from watch_assistant.api.strm import router as strm_router
from watch_assistant.api.subscriptions import router as subscriptions_router
from watch_assistant.api.subtitles import router as subtitles_router
from watch_assistant.api.tasks import router as tasks_router
from watch_assistant.api.telemetry import router as telemetry_router
from watch_assistant.api.webhooks import router as webhooks_router
from watch_assistant.api.workflows import router as workflows_router
from watch_assistant.config import Settings
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import Database, create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.release_metadata import read_release_commit
from watch_assistant.schemas import LoggingLevel
from watch_assistant.security import SecurityManager
from watch_assistant.services.agent_tokens import AgentTokenService
from watch_assistant.services.api_errors import (
    build_error_payload,
    error_code_from_detail,
    legacy_detail,
)
from watch_assistant.services.backups import BackupService
from watch_assistant.services.cache_warm import CacheWarmer
from watch_assistant.services.credentials import CredentialService
from watch_assistant.services.deployment_diagnostics import DeploymentDiagnosticsService
from watch_assistant.services.directory_dirty_worker import DirectoryDirtyWorker
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupError,
    EmptyDirectoryCleanupStatus,
    LiveP115EmptyDirectoryCleaner,
)
from watch_assistant.services.empty_directory_cleanup_plan import (
    EmptyDirectoryCleanupPlanService,
)
from watch_assistant.services.inspection import InspectionService, InspectionWorker
from watch_assistant.services.inventory_push_guard import (
    InventoryPushGuard,
    InventoryRefreshEvidence,
)
from watch_assistant.services.library_index import LibraryIndexService
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationService,
    LibraryScanWorker,
)
from watch_assistant.services.maintenance import MaintenanceService
from watch_assistant.services.managed_directory_ownership import (
    ManagedDirectoryOwnershipService,
)
from watch_assistant.services.manual_import import ManualImportService
from watch_assistant.services.mcp import McpService
from watch_assistant.services.notifications import NotificationService
from watch_assistant.services.observability import emit_event
from watch_assistant.services.organization_automation import (
    OrganizationAutomationService,
)
from watch_assistant.services.organization_capability import (
    organization_execution_supported,
)
from watch_assistant.services.organization_directory_provisioner import (
    OrganizationDirectoryProvisioner,
)
from watch_assistant.services.organization_history import OrganizationHistoryService
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
)
from watch_assistant.services.organization_plan import OrganizationPlanService
from watch_assistant.services.organization_preview import OrganizationPreviewService
from watch_assistant.services.organization_scheduler import OrganizationScheduler
from watch_assistant.services.organization_target import read_target_catalog
from watch_assistant.services.organization_worker import (
    OrganizationWorker,
    _close_client,
    _default_client_factory,
)
from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
)
from watch_assistant.services.p115_delete import P115DeleteService
from watch_assistant.services.p115_login_devices import P115LoginDeviceService
from watch_assistant.services.p115_qrcode import P115QrcodeService
from watch_assistant.services.p115_settings import P115SettingsService
from watch_assistant.services.prowlarr_settings import ProwlarrSettingsService
from watch_assistant.services.pwa_devices import PwaDeviceService
from watch_assistant.services.quality_profiles import QualityProfileService
from watch_assistant.services.search import SearchService
from watch_assistant.services.season_metadata import SeasonMetadataService
from watch_assistant.services.settings import SettingsService
from watch_assistant.services.strm_manifest import StrmManifestService
from watch_assistant.services.strm_operations import StrmOperationService
from watch_assistant.services.subscription_scheduler import SubscriptionScheduler
from watch_assistant.services.subscriptions import SubscriptionService
from watch_assistant.services.tasks import TaskService
from watch_assistant.services.webhooks import WebhookService
from watch_assistant.services.workflows import WorkflowService
from watch_assistant.worker import TaskAdapter, TaskWorker

_RELEASE_SHA = re.compile(r"(?:[0-9a-f]{7}|[0-9a-f]{40})", re.IGNORECASE)
_TRUSTED_RELEASE_PATH = Path("/opt/watch-assistant/current")
_DEFAULT_PLAYBACK_NETWORKS = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "fc00::/7",
)
_STRM_OPERATION_RECOVERY_INTERVAL_SECONDS = 60.0
_STRM_OPERATION_STALE_AFTER = timedelta(minutes=30)


async def _refresh_inventory_before_push(
    application: FastAPI,
) -> InventoryRefreshEvidence:
    """Refresh every enabled inventory scope using read-only 115 access."""
    database = getattr(application.state, "database", None)
    provider = getattr(application.state, "organization_cookie_provider", None)
    event_logger = getattr(application.state, "settings_service", None)
    if database is None or provider is None:
        await emit_event(
            event_logger,
            "inventory.refresh.failed",
            level=LoggingLevel.WARNING,
            fields={
                "hidden_count": 1,
                "error_code": "inventory_refresh_unavailable",
            },
        )
        return InventoryRefreshEvidence(
            complete=False,
            scope_verified=False,
            error_code="inventory_check_failed",
        )

    async with database.session_factory() as session:
        libraries = list(
            (
                await session.scalars(
                    select(MediaLibrary)
                    .where(MediaLibrary.enabled.is_(True))
                    .order_by(MediaLibrary.id)
                )
            ).all()
        )
    if not libraries:
        await emit_event(
            event_logger,
            "inventory.refresh.failed",
            level=LoggingLevel.WARNING,
            fields={
                "hidden_count": 1,
                "error_code": "inventory_scope_unconfigured",
            },
        )
        return InventoryRefreshEvidence(
            complete=False,
            scope_verified=False,
            error_code="inventory_scope_unconfigured",
        )

    await emit_event(
        event_logger,
        "inventory.refresh.started",
        fields={"total": len(libraries)},
    )
    for library in libraries:
        if not library.scope_verified:
            await emit_event(
                event_logger,
                "inventory.refresh.failed",
                level=LoggingLevel.WARNING,
                fields={
                    "hidden_count": 1,
                    "error_code": "inventory_scope_unconfigured",
                    "complete": False,
                    "scope_verified": False,
                },
            )
            return InventoryRefreshEvidence(
                complete=False,
                scope_verified=False,
                error_code="inventory_scope_unconfigured",
                library_count=len(libraries),
            )
        try:
            gateway = P115ReadOnlyDirectoryGateway(
                provider,
                authorized_directory_ids=(library.root_directory_id,),
                request_timeout_seconds=30,
            )
            result = await LibraryIndexService(
                database.session_factory,
                gateway,
                library_id=library.id,
                root_directory_id=library.root_directory_id,
                page_size=1,
                hydrate_file_details=True,
            ).scan_tree(f"push-inventory-{uuid4().hex}")
        except Exception:  # noqa: BLE001 - remote details stay private
            await emit_event(
                event_logger,
                "inventory.refresh.failed",
                level=LoggingLevel.WARNING,
                fields={
                    "hidden_count": 1,
                    "error_code": "inventory_refresh_failed",
                },
            )
            return InventoryRefreshEvidence(
                complete=False,
                scope_verified=False,
                error_code="inventory_check_failed",
                library_count=len(libraries),
            )
        if not result.complete or result.state.value != "completed":
            await emit_event(
                event_logger,
                "inventory.refresh.failed",
                level=LoggingLevel.WARNING,
                fields={
                    "hidden_count": 1,
                    "error_code": result.error_code or "inventory_index_incomplete",
                },
            )
            return InventoryRefreshEvidence(
                complete=False,
                scope_verified=True,
                error_code="inventory_index_incomplete",
                library_count=len(libraries),
            )
        async with database.session_factory() as session:
            current = await session.get(MediaLibrary, library.id)
        if (
            current is None
            or not current.enabled
            or not current.scope_verified
            or current.root_directory_id != library.root_directory_id
        ):
            await emit_event(
                event_logger,
                "inventory.refresh.failed",
                level=LoggingLevel.WARNING,
                fields={
                    "hidden_count": 1,
                    "error_code": "inventory_scope_unconfigured",
                    "complete": False,
                    "scope_verified": False,
                },
            )
            return InventoryRefreshEvidence(
                complete=False,
                scope_verified=False,
                error_code="inventory_scope_unconfigured",
                library_count=len(libraries),
            )

    await emit_event(
        event_logger,
        "inventory.refresh.completed",
        fields={"count": len(libraries)},
    )
    return InventoryRefreshEvidence(
        complete=True,
        scope_verified=True,
        library_count=len(libraries),
        refreshed_count=len(libraries),
    )


async def _current_managed_directory_ids(
    session_factory,
    directory_id: str,
    parent_id: str,
    library_id: str | None = None,
) -> frozenset[str]:
    """Return one library's latest complete directory scope for a candidate."""

    async with session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(LibraryScanRun)
                    .join(MediaLibrary, MediaLibrary.id == LibraryScanRun.library_id)
                    .where(
                        MediaLibrary.enabled.is_(True),
                        MediaLibrary.scope_verified.is_(True),
                        LibraryScanRun.root_directory_id
                        == MediaLibrary.root_directory_id,
                        LibraryScanRun.state == "completed",
                        LibraryScanRun.complete.is_(True),
                        LibraryScanRun.snapshot_revision.is_not(None),
                        *(
                            (MediaLibrary.id == library_id,)
                            if library_id is not None
                            else ()
                        ),
                    )
                )
            ).all()
        )
        latest: dict[str, LibraryScanRun] = {}
        for run in runs:
            current = latest.get(run.library_id)
            if current is None or (
                run.snapshot_revision or -1,
                run.id,
            ) > (
                current.snapshot_revision or -1,
                current.id,
            ):
                latest[run.library_id] = run
        for run in latest.values():
            directory_ids = set(
                (
                    await session.scalars(
                        select(LibraryScanEntry.object_id).where(
                            LibraryScanEntry.scan_run_id == run.id,
                            LibraryScanEntry.is_directory.is_(True),
                        )
                    )
                ).all()
            )
            scope = directory_ids | {run.root_directory_id}
            if directory_id in scope and parent_id in scope:
                return frozenset(scope)
    return frozenset()


def create_app(
    *,
    database: Database | None = None,
    crypto: SecretCrypto | None = None,
    tmdb_client: TmdbClient | None = None,
    pansou_client: PanSouClient | None = None,
    prowlarr_client: ProwlarrClient | None = None,
    security_manager: SecurityManager | None = None,
    share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    push_supported: bool | None = None,
    qbittorrent_client: QbittorrentClient | None = None,
    task_adapter: TaskAdapter | None = None,
    inventory_guard: InventoryPushGuard | None = None,
    frontend_dir: Path | None = None,
    release_root: Path | None = None,
    organization_plan_enabled: bool | None = None,
    organization_execution_enabled: bool | None = None,
    organization_write_enabled: bool | None = None,
    organization_write_contract_verified: bool | None = None,
    organization_contract: P115OrganizationContract | None = None,
    permanent_delete_enabled: bool | None = None,
    permanent_delete_contract_verified: bool | None = None,
    strm_full_enabled: bool | None = None,
    strm_incremental_enabled: bool | None = None,
    strm_cleanup_enabled: bool | None = None,
    strm_playback_enabled: bool | None = None,
    strm_playback_contract_verified: bool | None = None,
    strm_output_root: Path | None = None,
    strm_playback_url_prefix: str | None = None,
    strm_playback_allowed_networks: tuple[str, ...] | None = None,
    strm_playback_gateway: P115PlaybackGateway | None = None,
    system_created_directory_ids: Collection[str] | None = None,
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
        subscription_stop: asyncio.Event | None = None
        subscription_task: asyncio.Task[None] | None = None
        webhook_stop: asyncio.Event | None = None
        webhook_task: asyncio.Task[None] | None = None
        organization_stop: asyncio.Event | None = None
        organization_task: asyncio.Task[None] | None = None
        organization_worker_stop: asyncio.Event | None = None
        organization_worker_task: asyncio.Task[None] | None = None
        dirty_stop: asyncio.Event | None = None
        dirty_task: asyncio.Task[None] | None = None
        strm_recovery_stop: asyncio.Event | None = None
        strm_recovery_task: asyncio.Task[None] | None = None
        library_scan_stop: asyncio.Event | None = None
        library_scan_task: asyncio.Task[None] | None = None

        async def apply_dirty_runtime(ready: bool) -> None:
            nonlocal dirty_stop, dirty_task
            enabled = (
                ready
                and getattr(application.state, "strm_full_enabled", False)
                and getattr(application.state, "strm_incremental_enabled", False)
                and getattr(application.state, "organization_cookie_provider", None)
                is not None
            )
            if not enabled:
                if dirty_stop is not None:
                    dirty_stop.set()
                if dirty_task is not None:
                    dirty_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await dirty_task
                dirty_stop = None
                dirty_task = None
                if hasattr(application.state, "directory_dirty_worker"):
                    delattr(application.state, "directory_dirty_worker")
                return
            if dirty_task is not None:
                return

            def index_factory(library_id: str, root_directory_id: str) -> LibraryIndexService:
                gateway = P115ReadOnlyDirectoryGateway(
                    application.state.organization_cookie_provider,
                    authorized_directory_ids=(root_directory_id,),
                    request_timeout_seconds=30,
                )
                return LibraryIndexService(
                    application.state.database.session_factory,
                    gateway,
                    library_id=library_id,
                    root_directory_id=root_directory_id,
                    page_size=1,
                    hydrate_file_details=True,
                )

            worker = DirectoryDirtyWorker(
                application.state.database.session_factory,
                application.state.strm_manifest_service,
                index_factory,
                output_root=application.state.strm_output_root,
                playback_url_prefix=application.state.strm_playback_url_prefix,
                cleanup_enabled=bool(
                    getattr(application.state, "strm_cleanup_enabled", False)
                ),
                operation_service=application.state.strm_operation_service,
                settings_service=application.state.settings_service,
                # Empty-directory recycling is only authorized by the explicit
                # preview/apply API, never by the background dirty worker.
                empty_directory_cleaner=None,
                event_logger=application.state.settings_service,
            )
            application.state.directory_dirty_worker = worker
            dirty_stop = asyncio.Event()
            dirty_task = asyncio.create_task(
                worker.run_forever(dirty_stop),
                name="watch-assistant-directory-dirty-worker",
            )

        async def stop_organization_runtime() -> None:
            nonlocal organization_stop, organization_task
            nonlocal organization_worker_stop, organization_worker_task
            if organization_stop is not None:
                organization_stop.set()
            if organization_task is not None:
                organization_task.cancel()
                with suppress(asyncio.CancelledError):
                    await organization_task
            if organization_worker_stop is not None:
                organization_worker_stop.set()
            if organization_worker_task is not None:
                organization_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await organization_worker_task
            organization_stop = None
            organization_task = None
            organization_worker_stop = None
            organization_worker_task = None
            if hasattr(application.state, "organization_worker"):
                delattr(application.state, "organization_worker")
            if hasattr(application.state, "organization_scheduler"):
                delattr(application.state, "organization_scheduler")
            if hasattr(application.state, "organization_automation_service"):
                delattr(application.state, "organization_automation_service")
            if hasattr(application.state, "_organization_runtime_write_enabled"):
                delattr(application.state, "_organization_runtime_write_enabled")
            if hasattr(application.state, "empty_directory_cleanup_executor"):
                delattr(application.state, "empty_directory_cleanup_executor")

        async def apply_organization_runtime(ready: bool) -> None:
            nonlocal organization_stop, organization_task
            nonlocal organization_worker_stop, organization_worker_task
            planning_enabled = (
                getattr(application.state, "organization_plan_enabled", False)
                and getattr(application.state, "organization_cookie_provider", None)
                is not None
                and getattr(application.state, "organization_target_root_id", None)
                is not None
            )
            if not planning_enabled:
                await stop_organization_runtime()
                return
            write_enabled = (
                ready
                and getattr(application.state, "organization_execution_enabled", False)
                and getattr(application.state, "organization_write_enabled", False)
                and getattr(
                    getattr(
                        application.state,
                        "organization_contract",
                        P115OrganizationContract(),
                    ),
                    "verified",
                    False,
                )
            )
            if (
                organization_task is not None
                and getattr(
                    application.state, "_organization_runtime_write_enabled", None
                )
                == write_enabled
            ):
                return
            if organization_task is not None:
                await stop_organization_runtime()
            worker = None
            directory_provisioner = None
            organization_contract = getattr(
                application.state,
                "organization_contract",
                P115OrganizationContract(),
            )
            if write_enabled:
                async def execute_empty_directory_cleanup(
                    candidate: dict[str, str], *, lease_check=None
                ):
                    try:
                        directory_id = candidate["directory_id"]
                        parent_id = candidate["parent_id"]
                        name = candidate["name"]
                        cookie = await asyncio.to_thread(
                            application.state.organization_cookie_provider.load
                        )
                        if not cookie:
                            raise EmptyDirectoryCleanupError("credentials_unavailable")
                        client = await asyncio.to_thread(_default_client_factory, cookie)
                        ownership_service = getattr(
                            application.state,
                            "managed_directory_ownership_service",
                            None,
                        )
                        if ownership_service is None:
                            raise EmptyDirectoryCleanupError(
                                "cleanup_scope_unverified"
                            )
                        ownership = await ownership_service.get_active(
                            directory_id,
                            parent_directory_id=parent_id,
                            name=name,
                            relative_path=candidate.get("path"),
                        )
                        if ownership is None:
                            raise EmptyDirectoryCleanupError(
                                "cleanup_scope_unverified"
                            )
                        managed_directory_ids = await _current_managed_directory_ids(
                            application.state.database.session_factory,
                            directory_id,
                            parent_id,
                            ownership.library_id,
                        )
                        if not managed_directory_ids:
                            raise EmptyDirectoryCleanupError(
                                "cleanup_scope_unverified"
                            )
                        cleaner = LiveP115EmptyDirectoryCleaner(
                            client=client,
                            call_executor=p115_c03_timeout_executor,
                            managed_directory_ids=managed_directory_ids,
                            system_created_directory_ids=(ownership.directory_id,),
                            scope_confirmed=True,
                        )
                        return await cleaner.cleanup(
                            directory_id,
                            parent_id,
                            name,
                            lease_check=lease_check,
                        )
                    except EmptyDirectoryCleanupError as error:
                        return (
                            EmptyDirectoryCleanupStatus.UNCERTAIN
                            if error.uncertain
                            else EmptyDirectoryCleanupStatus.FAILED
                        )
                    except Exception as exc:  # noqa: BLE001 - details stay private
                        del exc
                        raise EmptyDirectoryCleanupError(
                            "empty_directory_cleanup_unavailable"
                        ) from None
                    finally:
                        if "client" in locals():
                            await _close_client(client)

                application.state.empty_directory_cleanup_executor = (
                    execute_empty_directory_cleanup
                )
                async def provision_organization_directories(operation_id: str) -> None:
                    summary = await application.state.organization_operation_service.get(
                        operation_id
                    )
                    plan_scope = (
                        await application.state.organization_operation_service.plan_execution_scope(
                            operation_id
                        )
                    )
                    paths = await application.state.organization_plan_service.plan_target_directory_paths(
                        summary.plan_id
                    )
                    if not paths:
                        return
                    gateway = P115ReadOnlyDirectoryGateway(
                        application.state.organization_cookie_provider,
                        authorized_directory_ids=(
                            application.state.organization_target_root_id,
                        ),
                        request_timeout_seconds=30,
                    )
                    catalog = await read_target_catalog(
                        gateway, application.state.organization_target_root_id
                    )
                    missing_paths = tuple(
                        path for path in paths if path not in catalog.by_path
                    )
                    if not missing_paths:
                        return
                    cookie = await asyncio.to_thread(
                        application.state.organization_cookie_provider.load
                    )
                    if not cookie:
                        raise RuntimeError("credentials_unavailable")
                    client = await asyncio.to_thread(_default_client_factory, cookie)
                    try:
                        provisioner = OrganizationDirectoryProvisioner(
                            client,
                            call_executor=p115_c03_timeout_executor,
                            organization_contract=organization_contract,
                            ownership_service=application.state.managed_directory_ownership_service,
                            event_logger=application.state.settings_service,
                        )
                        await provisioner.ensure(
                            target_root_id=application.state.organization_target_root_id,
                            library_id=await application.state.organization_plan_service.plan_library_id(
                                summary.plan_id
                            ),
                            operation_id=operation_id,
                            existing_directories=catalog.by_path,
                            paths=missing_paths,
                            write_enabled=write_enabled,
                            plan_confirmed=plan_scope is not None,
                            scope_confirmed=(
                                plan_scope is not None
                                and application.state.organization_target_root_id
                                in plan_scope
                            ),
                            lease_active=True,
                        )
                    finally:
                        await _close_client(client)

                directory_provisioner = provision_organization_directories
                worker = OrganizationWorker(
                    application.state.database.session_factory,
                    application.state.organization_operation_service,
                    application.state.organization_cookie_provider,
                    production_root_id=application.state.organization_target_root_id,
                    live_enabled=True,
                    write_enabled=write_enabled,
                    event_logger=application.state.settings_service,
                    settings_service=application.state.settings_service,
                    organization_contract=organization_contract,
                    directory_provisioner=directory_provisioner,
                )
                application.state.organization_worker = worker
            elif hasattr(application.state, "empty_directory_cleanup_executor"):
                delattr(application.state, "empty_directory_cleanup_executor")

            def gateway_factory(directory_ids):
                return P115ReadOnlyDirectoryGateway(
                    application.state.organization_cookie_provider,
                    authorized_directory_ids=tuple(directory_ids),
                    request_timeout_seconds=30,
                )

            automation = OrganizationAutomationService(
                application.state.database.session_factory,
                application.state.settings_service,
                application.state.organization_preview_service,
                application.state.organization_plan_service,
                gateway_factory,
                hydrate_file_details=True,
                event_logger=application.state.settings_service,
            )
            application.state.organization_automation_service = automation
            application.state._organization_runtime_write_enabled = write_enabled

            async def run_organization_once() -> bool:
                return await automation.run_once()

            async def run_organization_manual(run_id: str) -> bool:
                return await automation.run_once(
                    manual_confirmation=True, run_id=run_id
                )

            scheduler = OrganizationScheduler(
                application.state.settings_service,
                run_organization_once,
                run_organization_manual,
            )
            application.state.organization_scheduler = scheduler
            organization_stop = asyncio.Event()
            organization_task = asyncio.create_task(
                scheduler.run_forever(organization_stop),
                name="watch-assistant-organization-scheduler",
            )
            if worker is not None:
                organization_worker_stop = asyncio.Event()
                organization_worker_task = asyncio.create_task(
                    worker.run_forever(organization_worker_stop),
                    name="watch-assistant-organization-worker",
                )

        async def apply_p115_runtime(ready: bool) -> None:
            nonlocal task_stop, task_task
            application.state.p115_ready = ready
            application.state.strm_playback_supported = bool(
                ready and getattr(application.state, "strm_playback_gateway", None)
            )
            application.state.push_capabilities = {
                "magnet": ready,
                "share": False,
            }
            adapter = getattr(application.state, "task_adapter", None)
            if adapter is None:
                await apply_organization_runtime(False)
                await apply_dirty_runtime(False)
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
                await apply_organization_runtime(False)
                await apply_dirty_runtime(False)
                return
            if getattr(application.state, "task_worker", None) is None:
                worker = TaskWorker(
                    runtime_database.session_factory,
                    runtime_crypto,
                    adapter,
                    owner=_worker_owner(),
                    event_logger=application.state.settings_service,
                    inventory_guard=getattr(
                        application.state, "inventory_push_guard", None
                    ),
                    inventory_refresh=lambda: _refresh_inventory_before_push(
                        application
                    ),
                )
                await worker.recover_expired()
                application.state.task_worker = worker
                task_stop = asyncio.Event()
                task_task = asyncio.create_task(
                    worker.run_forever(task_stop), name="watch-assistant-task-worker"
                )
            await apply_organization_runtime(True)
            await apply_dirty_runtime(True)

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
            if strm_playback_allowed_networks is None:
                application.state.strm_playback_allowed_networks = _parse_networks(
                    settings.strm_playback_allowed_networks
                )
            if organization_plan_enabled is None:
                application.state.organization_plan_enabled = (
                    settings.organization_plan_enabled
                )
            if organization_execution_enabled is None:
                application.state.organization_execution_enabled = (
                    settings.organization_execution_enabled
                )
            if settings.p115_enabled and settings.p115_target_cid is None:
                raise RuntimeError("P115_ENABLED requires P115_TARGET_CID")
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
            prowlarr_settings_service = ProwlarrSettingsService(
                runtime_database.session_factory,
                runtime_crypto,
                environment_enabled=settings.prowlarr_enabled,
                environment_base_url=settings.prowlarr_base_url,
                environment_api_key=settings.prowlarr_api_key.get_secret_value(),
                environment_allowed_private_addresses=(
                    settings.prowlarr_allowed_private_addresses
                ),
                timeout_seconds=settings.prowlarr_timeout_seconds,
                event_logger=application.state.settings_service,
                runtime_state=application.state,
            )
            application.state.prowlarr_settings_service = prowlarr_settings_service
            runtime_prowlarr = prowlarr_client or await (
                prowlarr_settings_service.runtime_client()
            )
            application.state.webhook_service = WebhookService(
                runtime_database.session_factory,
                runtime_crypto,
                event_logger=application.state.settings_service,
            )
            application.state.settings_service.bind_event_sink(
                application.state.webhook_service.enqueue_event
            )
            application.state.pwa_device_service = PwaDeviceService(
                runtime_database.session_factory, runtime_crypto
            )
            application.state.p115_login_device_service = P115LoginDeviceService(
                runtime_database.session_factory, runtime_crypto
            )
            application.state.p115_qrcode_service = P115QrcodeService()
            application.state.agent_token_service = AgentTokenService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.organization_plan_service = OrganizationPlanService(
                runtime_database.session_factory
            )
            application.state.managed_directory_ownership_service = (
                ManagedDirectoryOwnershipService(runtime_database.session_factory)
            )
            application.state.library_scan_operation_service = (
                LibraryScanOperationService(
                    runtime_database.session_factory,
                    event_logger=application.state.settings_service,
                )
            )
            application.state.organization_history_service = OrganizationHistoryService(
                runtime_database.session_factory
            )
            application.state.organization_operation_service = (
                OrganizationOperationService(
                    runtime_database.session_factory,
                    event_logger=application.state.settings_service,
                )
            )
            application.state.strm_manifest_service = StrmManifestService(
                runtime_database.session_factory,
                managed_output_roots=(application.state.strm_output_root,),
            )
            fallback_cookie_provider = CookieProvider(settings.p115_cookie_path)
            composite_cookie_provider = CompositeCookieProvider(
                fallback_cookie_provider
            )
            active_device_cookie = await application.state.p115_login_device_service.active_cookie()
            if active_device_cookie is not None:
                composite_cookie_provider.set_managed(active_device_cookie)
            if application.state.strm_playback_gateway is None and settings.p115_enabled:
                application.state.strm_playback_gateway = P115LivePlaybackGateway(
                    runtime_database.session_factory,
                    composite_cookie_provider,
                    max_concurrency=min(settings.p115_max_concurrency, 2),
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
            if active_device_cookie is not None:
                composite_cookie_provider.set_managed(active_device_cookie)
            runtime_tmdb = tmdb_client or TmdbClient(
                managed_tmdb or settings.tmdb_api_key.get_secret_value(),
                base_url=settings.tmdb_base_url,
            )
            application.state.organization_plan_service.bind_candidate_search_client(
                runtime_tmdb
            )
            application.state.organization_preview_service = OrganizationPreviewService(
                runtime_database.session_factory,
                runtime_tmdb,
                application.state.organization_plan_service,
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
                prowlarr_client=runtime_prowlarr,
                crypto=runtime_crypto,
                share_domains=share_domains,
                pansou_max_concurrency=settings.pansou_max_concurrency,
                prowlarr_max_concurrency=settings.prowlarr_max_concurrency,
                event_logger=application.state.settings_service,
            )
            application.state.manual_import_service = ManualImportService(
                runtime_database.session_factory,
                application.state.search_service,
                runtime_crypto,
                share_domains=share_domains,
            )
            application.state.subscription_service = SubscriptionService(
                runtime_database.session_factory,
                application.state.search_service,
                event_logger=application.state.settings_service,
            )
            application.state.subscription_scheduler = SubscriptionScheduler(
                runtime_database.session_factory,
                application.state.subscription_service,
                event_logger=application.state.settings_service,
            )
            application.state.quality_profile_service = QualityProfileService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.workflow_service = WorkflowService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            await application.state.workflow_service.recover_stale()
            application.state.notification_service = NotificationService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.settings_service.bind_event_sink(
                application.state.notification_service.handle_event
            )
            application.state.backup_service = BackupService(
                runtime_database.engine.url.database,
                _state_directory(runtime_database) / "backups",
                release=application.state.release,
                event_logger=application.state.settings_service,
            )
            application.state.deployment_diagnostics_service = (
                DeploymentDiagnosticsService(runtime_database.engine, application.state)
            )
            application.state.season_metadata_service = SeasonMetadataService(
                runtime_database.session_factory, runtime_tmdb
            )
            application.state.task_service = TaskService(
                runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.mcp_service = McpService(
                task_service=application.state.task_service,
                notification_service=application.state.notification_service,
                organization_plan_service=application.state.organization_plan_service,
                organization_operation_service=application.state.organization_operation_service,
                workflow_service=application.state.workflow_service,
                library_session_factory=runtime_database.session_factory,
                event_logger=application.state.settings_service,
            )
            application.state.maintenance_service = MaintenanceService(
                runtime_database.session_factory
            )
            if security_manager is None:
                application.state.security_manager = SecurityManager(
                    web_password_hash=(
                        settings.web_password_hash.get_secret_value()
                        if settings.web_password_hash is not None
                        else None
                    ),
                    script_token_hash=settings.script_token_hash.get_secret_value(),
                    web_username=settings.web_username,
                    bootstrap_admin_enabled=settings.web_auth_bootstrap_enabled,
                    bootstrap_admin_password=(
                        settings.web_auth_bootstrap_password.get_secret_value()
                        if settings.web_auth_bootstrap_password is not None
                        else None
                    ),
                    diagnostics_token=settings.diagnostics_token.get_secret_value(),
                    cookie_secure=settings.cookie_secure,
                    session_factory=runtime_database.session_factory,
                    session_ttl=timedelta(hours=settings.web_session_ttl_hours),
                    event_logger=application.state.settings_service,
                )
            else:
                security_manager.configure_session_store(
                    runtime_database.session_factory,
                    session_ttl=timedelta(hours=settings.web_session_ttl_hours),
                )
                application.state.security_manager = security_manager
                security_manager.configure_event_logger(
                    application.state.settings_service
                )
            application.state.push_supported = False
            application.state.push_capabilities = {
                "magnet": False,
                "share": False,
            }
            application.state.p115_ready = False
            application.state.database = runtime_database
            application.state.strm_operation_service = StrmOperationService(
                runtime_database.session_factory
            )
            application.state.inventory_push_guard = (
                inventory_guard
                if inventory_guard is not None
                else InventoryPushGuard(runtime_database.session_factory)
            )
            application.state.organization_cookie_provider = composite_cookie_provider
            application.state.p115_directory_picker_root_id = "0"
            application.state.organization_target_root_id = (
                str(settings.p115_target_cid)
                if settings.p115_target_cid is not None and settings.p115_target_cid > 0
                else None
            )
            organization_settings = await application.state.settings_service.get_organization()
            configured_directory_ids = {
                directory_id
                for directory_id in (
                    *organization_settings.source_directory_ids,
                    organization_settings.target_directory_id,
                    organization_settings.push_directory_id,
                )
                if isinstance(directory_id, str) and directory_id.isdigit()
            }
            if application.state.organization_target_root_id:
                configured_directory_ids.add(application.state.organization_target_root_id)
            application.state.p115_browsed_directory_ids = configured_directory_ids
            application.state.p115_delete_service = P115DeleteService(
                runtime_database.session_factory,
                composite_cookie_provider,
            )
            owned = [runtime_database, runtime_tmdb, runtime_pansou]
            if runtime_prowlarr is not None:
                owned.append(runtime_prowlarr)
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
                application.state.strm_playback_supported = bool(
                    p115_ready and application.state.strm_playback_gateway is not None
                )
                if p115_ready:
                    application.state.task_worker = TaskWorker(
                        runtime_database.session_factory,
                        runtime_crypto,
                        runtime_task_adapter,
                        owner=_worker_owner(),
                        event_logger=application.state.settings_service,
                        inventory_guard=application.state.inventory_push_guard,
                        inventory_refresh=lambda: _refresh_inventory_before_push(
                            application
                        ),
                    )
                    application.state.push_capabilities = {
                        "magnet": True,
                        "share": False,
                    }
            else:
                application.state.strm_playback_supported = False
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
                    item_timeout=(
                        settings.inspection_final_timeout_seconds
                        or settings.inspection_item_timeout_seconds
                    ),
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
            if settings.subscription_scheduler_enabled:
                subscription_stop = asyncio.Event()
                subscription_task = asyncio.create_task(
                    application.state.subscription_scheduler.run_forever(
                        subscription_stop,
                        interval_seconds=settings.subscription_scheduler_interval_seconds,
                    ),
                    name="watch-assistant-subscription-scheduler",
                )
        operation_database = getattr(application.state, "database", None)
        if operation_database is not None:
            operation_service = getattr(
                application.state, "strm_operation_service", None
            )
            if not isinstance(operation_service, StrmOperationService):
                operation_service = StrmOperationService(
                    operation_database.session_factory
                )
                application.state.strm_operation_service = operation_service
            await operation_service.recover_incomplete()
            await EmptyDirectoryCleanupPlanService(
                operation_database.session_factory
            ).recover_stale_applying()
            strm_recovery_stop = asyncio.Event()

            async def run_strm_operation_recovery() -> None:
                while not strm_recovery_stop.is_set():
                    try:
                        await operation_service.recover_stale(
                            max_age=_STRM_OPERATION_STALE_AFTER
                        )
                    except Exception:  # noqa: BLE001 - recovery retries next tick
                        await asyncio.sleep(_STRM_OPERATION_RECOVERY_INTERVAL_SECONDS)
                        continue
                    try:
                        await asyncio.wait_for(
                            strm_recovery_stop.wait(),
                            timeout=_STRM_OPERATION_RECOVERY_INTERVAL_SECONDS,
                        )
                    except TimeoutError:
                        continue

            strm_recovery_task = asyncio.create_task(
                run_strm_operation_recovery(),
                name="watch-assistant-strm-operation-recovery",
            )

        scan_database = getattr(application.state, "database", None)
        scan_provider = getattr(application.state, "organization_cookie_provider", None)
        if scan_database is not None and scan_provider is not None:
            scan_operations = getattr(
                application.state, "library_scan_operation_service", None
            )
            if not isinstance(scan_operations, LibraryScanOperationService):
                scan_operations = LibraryScanOperationService(
                    scan_database.session_factory,
                    event_logger=getattr(application.state, "settings_service", None),
                )
                application.state.library_scan_operation_service = scan_operations

            def scan_gateway_factory(
                root_directory_id: str, authorized_directory_ids: Collection[str]
            ):
                return P115ReadOnlyDirectoryGateway(
                    scan_provider,
                    authorized_directory_ids=tuple(authorized_directory_ids),
                    request_timeout_seconds=30,
                )

            scan_worker = LibraryScanWorker(
                scan_database.session_factory,
                scan_operations,
                scan_gateway_factory,
                owner=_worker_owner(),
                hydrate_file_details=True,
                event_logger=getattr(application.state, "settings_service", None),
            )
            await scan_worker.recover_expired()
            application.state.library_scan_worker = scan_worker
            library_scan_stop = asyncio.Event()
            library_scan_task = asyncio.create_task(
                scan_worker.run_forever(library_scan_stop),
                name="watch-assistant-library-scan-worker",
            )

        task_adapter_resource = getattr(application.state, "task_adapter", None)
        if task_adapter_resource is not None and not hasattr(
            application.state, "p115_ready"
        ):
            p115_ready = await _ensure_adapter_available(task_adapter_resource)
            application.state.p115_ready = p115_ready
            application.state.strm_playback_supported = bool(
                p115_ready and application.state.strm_playback_gateway is not None
            )
            if not p115_ready:
                application.state.push_capabilities = {
                    "magnet": False,
                    "share": False,
                }
                application.state.task_worker = None
        elif (
            not getattr(application.state, "p115_ready", False)
            and application.state.strm_playback_gateway is None
        ):
            application.state.strm_playback_supported = False
        await apply_organization_runtime(
            bool(getattr(application.state, "p115_ready", False))
        )
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
        webhook_service = getattr(application.state, "webhook_service", None)
        if webhook_service is not None:
            webhook_stop = asyncio.Event()

            async def run_webhook_worker() -> None:
                while not webhook_stop.is_set():
                    try:
                        await webhook_service.publish_due()
                    except Exception:  # noqa: BLE001 - one delivery must not stop the worker
                        await asyncio.sleep(5)
                        continue
                    try:
                        await asyncio.wait_for(webhook_stop.wait(), timeout=5)
                    except TimeoutError:
                        continue

            webhook_task = asyncio.create_task(
                run_webhook_worker(), name="watch-assistant-webhook-worker"
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
            if dirty_task is not None and dirty_stop is not None:
                dirty_stop.set()
                dirty_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dirty_task
            if strm_recovery_task is not None and strm_recovery_stop is not None:
                strm_recovery_stop.set()
                strm_recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await strm_recovery_task
            if library_scan_task is not None and library_scan_stop is not None:
                library_scan_stop.set()
                library_scan_task.cancel()
                with suppress(asyncio.CancelledError):
                    await library_scan_task
            if organization_task is not None and organization_stop is not None:
                organization_stop.set()
                organization_task.cancel()
                with suppress(asyncio.CancelledError):
                    await organization_task
            if (
                organization_worker_task is not None
                and organization_worker_stop is not None
            ):
                organization_worker_stop.set()
                organization_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await organization_worker_task
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
            if subscription_task is not None and subscription_stop is not None:
                subscription_stop.set()
                subscription_task.cancel()
                with suppress(asyncio.CancelledError):
                    await subscription_task
            if webhook_task is not None and webhook_stop is not None:
                webhook_stop.set()
                webhook_task.cancel()
                with suppress(asyncio.CancelledError):
                    await webhook_task
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
            search_service = getattr(application.state, "search_service", None)
            active_prowlarr = getattr(search_service, "_prowlarr", None)
            if (
                active_prowlarr is not None
                and active_prowlarr not in owned
                and hasattr(active_prowlarr, "aclose")
            ):
                await active_prowlarr.aclose()
            webhook_service = getattr(application.state, "webhook_service", None)
            if webhook_service is not None and webhook_service not in owned:
                await webhook_service.aclose()

    application = FastAPI(title="Watch Assistant", lifespan=lifespan)

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = _request_context_id(request.headers.get("X-Request-ID"), "req_")
        correlation_id = _request_context_id(
            request.headers.get("X-Correlation-ID"), "corr_"
        )
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @application.exception_handler(HTTPException)
    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exception: HTTPException):
        request_id, correlation_id = _request_context_values(request)
        code = error_code_from_detail(exception.detail, exception.status_code)
        payload = build_error_payload(
            code,
            exception.status_code,
            request_id=request_id,
            correlation_id=correlation_id,
            missing_scopes=(
                exception.detail.get("missing_scopes", [])
                if isinstance(exception.detail, dict)
                else None
            ),
        )
        return JSONResponse(
            status_code=exception.status_code,
            content={"error": payload, "detail": jsonable_encoder(legacy_detail(exception.detail, code))},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exception: RequestValidationError):
        request_id, correlation_id = _request_context_values(request)
        fields = _validation_field_errors(exception)
        payload = build_error_payload(
            "validation_error",
            422,
            request_id=request_id,
            correlation_id=correlation_id,
            field_errors=fields,
        )
        return JSONResponse(status_code=422, content={"error": payload, "detail": "validation_error"})

    @application.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, _exception: Exception):
        request_id, correlation_id = _request_context_values(request)
        payload = build_error_payload(
            "internal_error",
            500,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return JSONResponse(status_code=500, content={"error": payload, "detail": "internal_error"})
    # Only offline startup smoke injects an extracted release root. Production
    # keeps the trusted /opt/watch-assistant/current/VERSION default.
    application.state.release = _resolve_release(release_root)
    application.state.started_at = time.monotonic()
    application.state.organization_plan_enabled = (
        organization_plan_enabled
        if organization_plan_enabled is not None
        else _env_flag("ORGANIZATION_PLAN_ENABLED")
    )
    application.state.organization_execution_enabled = (
        organization_execution_enabled
        if organization_execution_enabled is not None
        else _env_flag("ORGANIZATION_EXECUTION_ENABLED")
    )
    application.state.organization_write_enabled = (
        organization_write_enabled
        if organization_write_enabled is not None
        else _env_flag("ORGANIZATION_WRITE_ENABLED")
    )
    if organization_contract is not None and not isinstance(
        organization_contract, P115OrganizationContract
    ):
        raise ValueError("invalid_organization_contract")
    application.state.organization_contract = (
        organization_contract or P115OrganizationContract()
    )
    # The legacy environment flag is intentionally ignored as write evidence.
    # Only an explicitly injected, versioned contract can make this true.
    del organization_write_contract_verified
    application.state.organization_write_contract_verified = bool(
        application.state.organization_contract.verified
        and application.state.organization_contract.evidence is not None
    )
    application.state.permanent_delete_enabled = (
        permanent_delete_enabled
        if permanent_delete_enabled is not None
        else _env_flag("PERMANENT_DELETE_ENABLED")
    )
    application.state.permanent_delete_contract_verified = (
        permanent_delete_contract_verified
        if permanent_delete_contract_verified is not None
        else _env_flag("PERMANENT_DELETE_CONTRACT_VERIFIED")
    )
    application.state.strm_full_enabled = (
        strm_full_enabled
        if strm_full_enabled is not None
        else _env_flag("STRM_FULL_ENABLED")
    )
    application.state.strm_incremental_enabled = (
        strm_incremental_enabled
        if strm_incremental_enabled is not None
        else _env_flag("STRM_INCREMENTAL_ENABLED")
    ) and bool(application.state.strm_full_enabled)
    application.state.strm_cleanup_enabled = (
        strm_cleanup_enabled
        if strm_cleanup_enabled is not None
        else _env_flag("STRM_CLEANUP_ENABLED")
    ) and bool(application.state.strm_full_enabled)
    application.state.strm_playback_enabled = (
        strm_playback_enabled
        if strm_playback_enabled is not None
        else _env_flag("STRM_PLAYBACK_ENABLED")
    )
    application.state.strm_playback_contract_verified = (
        strm_playback_contract_verified
        if strm_playback_contract_verified is not None
        else _env_flag("STRM_PLAYBACK_CONTRACT_VERIFIED")
    )
    application.state.strm_playback_gateway = strm_playback_gateway
    application.state.strm_playback_supported = strm_playback_gateway is not None
    # Keep validating the legacy argument for callers, but never use process
    # memory as proof that a remote directory is owned by this system.
    if system_created_directory_ids is not None:
        if isinstance(system_created_directory_ids, (str, bytes)):
            raise TypeError("invalid_system_created_directory_ids")
        if any(
            not isinstance(item, str)
            or not item.isascii()
            or not item.isdigit()
            or item.startswith("0")
            for item in system_created_directory_ids
        ):
            raise ValueError("invalid_system_created_directory_ids")
    application.state.strm_output_root = strm_output_root or Path(
        os.environ.get("STRM_OUTPUT_ROOT", "./data/strm")
    )
    application.state.strm_playback_url_prefix = strm_playback_url_prefix or os.environ.get(
        "STRM_PLAYBACK_URL_PREFIX",
        "http://127.0.0.1:8115/api/v1/strm/play",
    )
    application.state.strm_playback_allowed_networks = _parse_networks(
        strm_playback_allowed_networks
        or os.environ.get("STRM_PLAYBACK_ALLOWED_NETWORKS")
        or _DEFAULT_PLAYBACK_NETWORKS
    )
    if database and crypto and tmdb_client and pansou_client:
        application.state.database = database
        application.state.settings_service = SettingsService(
            database.session_factory,
            state_directory=_state_directory(database),
        )
        application.state.webhook_service = WebhookService(
            database.session_factory,
            crypto,
            event_logger=application.state.settings_service,
        )
        application.state.settings_service.bind_event_sink(
            application.state.webhook_service.enqueue_event
        )
        application.state.pwa_device_service = PwaDeviceService(
            database.session_factory, crypto
        )
        application.state.agent_token_service = AgentTokenService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.prowlarr_settings_service = ProwlarrSettingsService(
            database.session_factory,
            crypto,
            environment_enabled=_env_flag("PROWLARR_ENABLED"),
            environment_base_url=os.environ.get("PROWLARR_BASE_URL", ""),
            environment_api_key=os.environ.get("PROWLARR_API_KEY", ""),
            environment_allowed_private_addresses=os.environ.get(
                "PROWLARR_ALLOWED_PRIVATE_ADDRESSES", ""
            ),
            timeout_seconds=float(os.environ.get("PROWLARR_TIMEOUT_SECONDS", "12")),
            event_logger=application.state.settings_service,
            runtime_state=application.state,
        )
        application.state.search_service = SearchService(
            database.session_factory,
            tmdb_client=tmdb_client,
            pansou_client=pansou_client,
            prowlarr_client=prowlarr_client,
            crypto=crypto,
            share_domains=share_domains,
            event_logger=application.state.settings_service,
        )
        application.state.manual_import_service = ManualImportService(
            database.session_factory,
            application.state.search_service,
            crypto,
            share_domains=share_domains,
        )
        application.state.subscription_service = SubscriptionService(
            database.session_factory,
            application.state.search_service,
            event_logger=application.state.settings_service,
        )
        application.state.quality_profile_service = QualityProfileService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.workflow_service = WorkflowService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.notification_service = NotificationService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.settings_service.bind_event_sink(
            application.state.notification_service.handle_event
        )
        application.state.backup_service = BackupService(
            database.engine.url.database,
            _state_directory(database) / "backups",
            release=application.state.release,
            event_logger=application.state.settings_service,
        )
        application.state.deployment_diagnostics_service = DeploymentDiagnosticsService(
            database.engine, application.state
        )
        application.state.season_metadata_service = SeasonMetadataService(
            database.session_factory, tmdb_client
        )
        application.state.task_service = TaskService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.maintenance_service = MaintenanceService(
            database.session_factory
        )
        application.state.organization_plan_service = OrganizationPlanService(
            database.session_factory, tmdb_client=tmdb_client
        )
        application.state.managed_directory_ownership_service = (
            ManagedDirectoryOwnershipService(database.session_factory)
        )
        application.state.library_scan_operation_service = (
            LibraryScanOperationService(
                database.session_factory,
                event_logger=application.state.settings_service,
            )
        )
        application.state.organization_history_service = OrganizationHistoryService(
            database.session_factory
        )
        application.state.organization_preview_service = OrganizationPreviewService(
            database.session_factory,
            tmdb_client,
            application.state.organization_plan_service,
        )
        application.state.organization_operation_service = OrganizationOperationService(
            database.session_factory,
            event_logger=application.state.settings_service,
        )
        application.state.strm_manifest_service = StrmManifestService(
            database.session_factory,
            managed_output_roots=(application.state.strm_output_root,),
        )
        application.state.strm_operation_service = StrmOperationService(
            database.session_factory
        )
        application.state.mcp_service = McpService(
            task_service=application.state.task_service,
            notification_service=application.state.notification_service,
            organization_plan_service=application.state.organization_plan_service,
            organization_operation_service=application.state.organization_operation_service,
            workflow_service=application.state.workflow_service,
            library_session_factory=database.session_factory,
            event_logger=application.state.settings_service,
        )
        fallback_cookie_provider = CookieProvider(
            os.environ.get("P115_COOKIE_PATH", "/run/secrets/p115_cookie")
        )
        composite_cookie_provider = CompositeCookieProvider(fallback_cookie_provider)
        application.state.organization_cookie_provider = composite_cookie_provider
        if (
            application.state.strm_playback_gateway is None
            and task_adapter is not None
        ):
            application.state.strm_playback_gateway = P115LivePlaybackGateway(
                database.session_factory,
                composite_cookie_provider,
            )
        application.state.p115_delete_service = P115DeleteService(
            database.session_factory,
            composite_cookie_provider,
        )
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
            security_manager.configure_event_logger(application.state.settings_service)
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
                inventory_guard=getattr(
                    application.state, "inventory_push_guard", inventory_guard
                ),
                inventory_refresh=lambda: _refresh_inventory_before_push(application),
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
        strm_cleanup_enabled = bool(
            getattr(application.state, "strm_cleanup_enabled", False)
        )
        if strm_cleanup_enabled:
            strm_cleanup_capability = {
                "enabled": True,
                "reason_code": None,
                "reason_zh": "可用",
                "settings_section": "overview",
            }
        else:
            strm_cleanup_capability = {
                "enabled": False,
                "reason_code": "strm_cleanup_disabled",
                "reason_zh": "STRM 失效清理未启用，请检查部署功能开关。",
                "settings_section": "overview",
            }
        empty_cleanup_setting = False
        if settings_service is not None:
            try:
                empty_cleanup_setting = (
                    await settings_service.get_organization()
                ).cleanup_empty_directories
            except Exception:  # noqa: BLE001 - health remains conservative
                empty_cleanup_setting = False
        ownership_service = getattr(
            application.state, "managed_directory_ownership_service", None
        )
        has_ownership = False
        if ownership_service is not None:
            try:
                has_ownership = await ownership_service.has_active_records()
            except Exception:  # noqa: BLE001 - health remains conservative
                has_ownership = False
        empty_cleanup_enabled = bool(
            empty_cleanup_setting
            and has_ownership
            and callable(
                getattr(application.state, "empty_directory_cleanup_executor", None)
            )
        )
        if empty_cleanup_enabled:
            empty_cleanup_capability = {
                "enabled": True,
                "reason_code": None,
                "reason_zh": "可用",
                "settings_section": "organization",
            }
        else:
            empty_cleanup_capability = {
                "enabled": False,
                "reason_code": "empty_directory_cleanup_disabled",
                "reason_zh": (
                    "空目录回收未就绪，请前往自动整理设置检查开关和写入契约。"
                ),
                "settings_section": "organization",
            }
        return {
            "status": "ok",
            "release": application.state.release,
            "push_supported": getattr(application.state, "push_supported", False),
            "push_capabilities": capabilities,
            "inspection_supported": getattr(
                application.state, "inspection_supported", False
            ),
            "inspection_auto_start_enabled": inspection_auto_start_enabled,
            "organization_plan_enabled": bool(
                getattr(application.state, "organization_plan_enabled", False)
            ),
            "organization_execution_enabled": bool(
                getattr(application.state, "organization_execution_enabled", False)
            ),
            "organization_write_enabled": bool(
                getattr(application.state, "organization_write_enabled", False)
            ),
            "organization_write_contract_verified": bool(
                getattr(
                    application.state, "organization_write_contract_verified", False
                )
            ),
            "permanent_delete_enabled": bool(
                getattr(application.state, "permanent_delete_enabled", False)
            ),
            "permanent_delete_contract_verified": bool(
                getattr(
                    application.state, "permanent_delete_contract_verified", False
                )
            ),
            "organization_execution_supported": bool(
                organization_execution_supported(application)
            ),
            "library_scan_supported": bool(
                getattr(application.state, "library_scan_worker", None) is not None
            ),
            "strm_capabilities": {
                "full": bool(getattr(application.state, "strm_full_enabled", False)),
                "incremental": bool(
                    getattr(application.state, "strm_incremental_enabled", False)
                ),
                "cleanup": strm_cleanup_enabled,
                "cleanup_capability": strm_cleanup_capability,
                "playback": bool(
                    getattr(application.state, "strm_playback_enabled", False)
                    and getattr(application.state, "strm_playback_supported", False)
                    and getattr(
                        application.state, "strm_playback_contract_verified", False
                    )
                ),
                "playback_contract_verified": bool(
                    getattr(
                        application.state, "strm_playback_contract_verified", False
                    )
                ),
            },
            "organization_capabilities": {
                "empty_directory_cleanup": empty_cleanup_capability,
            },
        }

    application.include_router(search_router)
    application.include_router(search_sources_router)
    application.include_router(settings_router)
    application.include_router(credentials_router)
    application.include_router(deployment_router)
    application.include_router(p115_settings_router)
    application.include_router(seasons_router)
    application.include_router(subtitles_router)
    application.include_router(subscriptions_router)
    application.include_router(quality_profiles_router)
    application.include_router(workflows_router)
    application.include_router(notifications_router)
    application.include_router(backups_router)
    application.include_router(tasks_router)
    application.include_router(telemetry_router)
    application.include_router(auth_router)
    application.include_router(agent_router)
    application.include_router(audit_router)
    application.include_router(webhooks_router)
    application.include_router(mcp_router)
    application.include_router(pwa_router)
    application.include_router(maintenance_router)
    application.include_router(inspection_router)
    application.include_router(library_router)
    application.include_router(manual_import_router)
    application.include_router(organization_plan_router)
    application.include_router(organization_history_router)
    application.include_router(organization_operation_router)
    application.include_router(strm_router)
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
        @application.get("/library", include_in_schema=False)
        @application.get("/workflows", include_in_schema=False)
        @application.get("/notifications", include_in_schema=False)
        async def frontend_browse_route() -> FileResponse:
            return FileResponse(index_path)

        application.mount(
            "/", StaticFiles(directory=static_path, html=True), name="frontend"
        )
    return application


def _resolve_release(trusted_path: Path | None = None) -> str:
    release_root = trusted_path or _TRUSTED_RELEASE_PATH
    try:
        resolved_root = release_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        resolved_root = None
    if resolved_root is not None:
        version_release = read_release_commit(resolved_root / "VERSION")
        if version_release is not None:
            return version_release
    configured = os.environ.get("WATCH_ASSISTANT_RELEASE", "").strip()
    if _RELEASE_SHA.fullmatch(configured) is not None:
        return configured.lower()
    if resolved_root is None:
        return "unknown"
    release_name = resolved_root.name
    if _RELEASE_SHA.fullmatch(release_name) is not None:
        return release_name.lower()
    return "unknown"


def _worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _parse_networks(value: str | tuple[str, ...]) -> tuple[object, ...]:
    values = value.split(",") if isinstance(value, str) else list(value)
    try:
        networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in values
            if item.strip()
        )
    except ValueError as error:
        raise RuntimeError("invalid_strm_playback_allowed_networks") from error
    if not networks:
        raise RuntimeError("invalid_strm_playback_allowed_networks")
    return networks


def _request_context_id(value: str | None, prefix: str) -> str:
    if value and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        return value
    return prefix + uuid4().hex


def _request_context_values(request: Request) -> tuple[str, str]:
    return (
        getattr(request.state, "request_id", _request_context_id(None, "req_")),
        getattr(request.state, "correlation_id", _request_context_id(None, "corr_")),
    )


def _validation_field_errors(exception: RequestValidationError) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in exception.errors():
        location = tuple(
            str(part)
            for part in item.get("loc", ())
            if part not in {"body", "query", "path", "header"}
        )
        field_id = ".".join(location) or "request"
        if field_id in seen:
            continue
        seen.add(field_id)
        fields.append({"field_id": field_id, "message_zh": "字段内容格式不正确。"})
    return fields


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
