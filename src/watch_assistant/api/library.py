"""Bounded, authenticated read-only media library resources."""

import asyncio
import hashlib
import json
import re
from datetime import UTC
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from watch_assistant.adapters.p115_library import (
    LibraryContractError,
    ScanState,
    scan_directory,
)
from watch_assistant.adapters.p115_library_gateway import (
    MAX_SCOPE_VERIFICATION_PAGES,
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)
from watch_assistant.library_models import (
    LibraryInventoryEvent,
    LibraryMediaIdentity,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.schemas import (
    EmptyDirectoryCleanupPlanApplyRequest,
    EmptyDirectoryCleanupPlanApplyResponse,
    EmptyDirectoryCleanupPlanRequest,
    EmptyDirectoryCleanupPlanResponse,
    InventoryCheckResponse,
    InventoryDuplicateGroupResponse,
    InventoryEventListResponse,
    InventoryEventResponse,
    InventoryFreshnessResponse,
    InventoryIdentityPatch,
    InventoryIdentityResponse,
    LibraryDeleteRequest,
    LibraryDeleteResponse,
    LibraryInventoryResponse,
    LibraryScanListResponse,
    LibraryScanRequest,
    LibraryScanSummary,
    MediaEntryListResponse,
    MediaEntryResponse,
    MediaLibraryConfigurationPatch,
    MediaLibraryListResponse,
    MediaLibraryResponse,
    MediaLibraryVerificationResponse,
    OrganizationPlanResponse,
    OrganizationPreviewRequest,
)
from watch_assistant.security import AuthContext, require_api_auth, require_scope
from watch_assistant.services.api_errors import error_status
from watch_assistant.services.empty_directory_cleanup_plan import (
    EmptyDirectoryCleanupPlanError,
    EmptyDirectoryCleanupPlanService,
)
from watch_assistant.services.library_inventory import (
    FreshnessStatus,
    InventoryFile,
    InventorySnapshot,
    build_snapshot,
    calculate_freshness,
    check_inventory,
)
from watch_assistant.services.library_scan_operations import (
    LibraryScanOperationError,
    LibraryScanOperationNotFound,
    LibraryScanOperationService,
    LibraryScanOperationSummary,
    scan_error_message_zh,
    scan_state_message_zh,
)
from watch_assistant.services.library_snapshot import (
    scan_has_verified_evidence,
    verified_latest_scan,
)
from watch_assistant.services.organization_plan import OrganizationPlanError
from watch_assistant.services.organization_preview import (
    OrganizationPreviewError,
    OrganizationPreviewService,
)
from watch_assistant.services.organization_target import (
    OrganizationTargetError,
    read_target_catalog,
)
from watch_assistant.services.p115_delete import DeleteStatus, P115DeleteService
from watch_assistant.services.strm_operations import (
    StrmOperationError,
    StrmOperationKind,
    StrmOperationService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]
ReviewWriteDependency = Annotated[AuthContext, Depends(require_scope("review:write"))]
SettingsWriteDependency = Annotated[AuthContext, Depends(require_scope("settings:write"))]
LibraryReadDependency = Annotated[AuthContext, Depends(require_scope("library:read"))]
LibraryWriteDependency = Annotated[AuthContext, Depends(require_scope("library:write"))]
OrganizeWriteDependency = Annotated[AuthContext, Depends(require_scope("organize:execute"))]
_LIBRARY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _require_permanent_delete_enabled_state(request: Request) -> None:
    if not getattr(request.app.state, "organization_write_enabled", False):
        raise HTTPException(status_code=503, detail="organization_write_disabled")
    if not getattr(
        request.app.state, "organization_write_contract_verified", False
    ):
        raise HTTPException(status_code=503, detail="organization_write_unverified")
    if not getattr(request.app.state, "permanent_delete_enabled", False):
        raise HTTPException(status_code=503, detail="permanent_delete_disabled")
    if not getattr(request.app.state, "permanent_delete_contract_verified", False):
        raise HTTPException(status_code=503, detail="permanent_delete_unverified")


async def require_permanent_delete_enabled(request: Request) -> None:
    _require_permanent_delete_enabled_state(request)


def _stable_library_id(value: str) -> bool:
    return isinstance(value, str) and _LIBRARY_ID.fullmatch(value) is not None


def _allowed(context: AuthContext, library_id: str) -> bool:
    return not context.via_bearer or not context.library_ids or library_id in context.library_ids


def _media_id(object_type: str, object_id: str) -> str:
    return f"{object_type}:{object_id}"


def _scan_summary(run: LibraryScanRun | None) -> LibraryScanSummary | None:
    if run is None:
        return None
    return LibraryScanSummary(
        run_id=run.id,
        state=run.state,
        complete=run.complete,
        snapshot_revision=run.snapshot_revision,
        pages_read=run.pages_read,
        items_seen=run.items_seen,
        added_count=run.added_count,
        changed_count=run.changed_count,
        removed_count=run.removed_count,
        attempts=run.attempts,
        state_message_zh=scan_state_message_zh(run.state),
        error_code=run.error_code,
        error_message_zh=scan_error_message_zh(run.error_code),
        cancel_requested=run.cancel_requested,
    )


def _operation_summary(summary: LibraryScanOperationSummary) -> LibraryScanSummary:
    return LibraryScanSummary(
        run_id=summary.run_id,
        state=summary.state,
        complete=summary.complete,
        snapshot_revision=summary.snapshot_revision,
        pages_read=summary.pages_read,
        items_seen=summary.items_seen,
        added_count=summary.added_count,
        changed_count=summary.changed_count,
        removed_count=summary.removed_count,
        attempts=summary.attempts,
        state_message_zh=summary.state_message_zh,
        error_code=summary.error_code,
        error_message_zh=summary.error_message_zh,
        cancel_requested=summary.cancel_requested,
    )


def _scan_operation_service(request: Request) -> LibraryScanOperationService:
    service = getattr(request.app.state, "library_scan_operation_service", None)
    if isinstance(service, LibraryScanOperationService):
        return service
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "library_scan_unavailable",
                "message": "媒体库扫描服务暂不可用",
            },
        )
    service = LibraryScanOperationService(
        database.session_factory,
        event_logger=getattr(request.app.state, "settings_service", None),
    )
    request.app.state.library_scan_operation_service = service
    return service


def _scan_http_error(error: LibraryScanOperationError) -> HTTPException:
    messages = {
        "invalid_library_id": "媒体库标识无效",
        "invalid_scan_run_id": "扫描操作标识无效",
        "invalid_idempotency_key": "幂等标识无效",
        "invalid_directory_limit": "目录数量上限无效",
        "invalid_pagination": "分页参数无效",
        "invalid_lease_duration": "扫描租约参数无效",
        "library_scope_unverified": "媒体库范围尚未完成只读验证",
        "scan_mode_unsupported": "扫描模式暂不受支持",
        "checkpoint_invalid": "扫描断点无效，请重新发起扫描",
    }
    code = error.code
    message = messages.get(code, "媒体库扫描服务暂不可用")
    return HTTPException(
        status_code=error_status(code) if code in messages else 503,
        detail={"code": code, "message": message},
    )


async def _latest_scans(session, library_ids: set[str] | None = None) -> dict[str, LibraryScanRun]:
    query = select(MediaLibrary).where(
        MediaLibrary.enabled.is_(True),
        MediaLibrary.scope_verified.is_(True),
    )
    if library_ids:
        query = query.where(MediaLibrary.id.in_(library_ids))
    libraries = list((await session.scalars(query)).all())
    latest: dict[str, LibraryScanRun] = {}
    for library in libraries:
        run = await verified_latest_scan(session, library)
        if run is not None:
            latest[library.id] = run
    return latest


@router.get("/libraries", response_model=MediaLibraryListResponse)
async def list_libraries(
    request: Request,
    context: AuthDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MediaLibraryListResponse:
    async with request.app.state.database.session_factory() as session:
        query = select(MediaLibrary).order_by(MediaLibrary.id)
        if context.via_bearer and context.library_ids:
            query = query.where(MediaLibrary.id.in_(context.library_ids))
        libraries = list((await session.scalars(query.offset(cursor).limit(limit + 1))).all())
        latest = await _latest_scans(session, {item.id for item in libraries})
        has_more = len(libraries) > limit
        libraries = libraries[:limit]
        items = [
            MediaLibraryResponse(
                library_id=library.id,
                name=library.name,
                root_directory_id=library.root_directory_id,
                enabled=library.enabled,
                scope_verified=library.scope_verified,
                revision=library.revision,
                latest_scan=_scan_summary(latest.get(library.id)),
            )
            for library in libraries
        ]
    return MediaLibraryListResponse(items=items, next_cursor=cursor + limit if has_more else None)


@router.put(
    "/libraries/{library_id}/configuration",
    response_model=MediaLibraryResponse,
)
async def configure_library(
    library_id: str,
    payload: MediaLibraryConfigurationPatch,
    request: Request,
    context: SettingsWriteDependency,
) -> MediaLibraryResponse:
    """Declare the production root without claiming that it was scanned.

    The root must match the server-side P115 target.  A changed root always
    invalidates the previous verification and disables the scope until the
    read-only verification route succeeds.
    """
    if not _stable_library_id(library_id):
        raise HTTPException(status_code=422, detail="invalid_request")
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    target_root = getattr(request.app.state, "organization_target_root_id", None)
    if not target_root:
        raise HTTPException(status_code=503, detail="library_scope_unavailable")
    if payload.root_directory_id != str(target_root):
        raise HTTPException(status_code=409, detail="library_scope_mismatch")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            if payload.revision != 0:
                raise HTTPException(status_code=409, detail="library_configuration_conflict")
            library = MediaLibrary(
                id=library_id,
                name=payload.name,
                root_directory_id=payload.root_directory_id,
                scope_verified=False,
                enabled=False,
                revision=1,
            )
            session.add(library)
        else:
            if library.revision != payload.revision:
                raise HTTPException(status_code=409, detail="library_configuration_conflict")
            root_changed = library.root_directory_id != payload.root_directory_id
            library.name = payload.name
            library.root_directory_id = payload.root_directory_id
            library.revision += 1
            if root_changed:
                library.scope_verified = False
                library.enabled = False
        await session.commit()
        await session.refresh(library)
        latest = await _latest_scans(session, {library.id})
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "library.configuration.changed",
            fields={"status": "saved", "scope_verified": library.scope_verified},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="library",
            resource_id=library_id,
        )
    return MediaLibraryResponse(
        library_id=library.id,
        name=library.name,
        root_directory_id=library.root_directory_id,
        enabled=library.enabled,
        scope_verified=library.scope_verified,
        revision=library.revision,
        latest_scan=_scan_summary(latest.get(library.id)),
    )


@router.post(
    "/libraries/{library_id}/verify-scope",
    response_model=MediaLibraryVerificationResponse,
)
async def verify_library_scope(
    library_id: str,
    request: Request,
    context: SettingsWriteDependency,
) -> MediaLibraryVerificationResponse:
    """Verify one page through the read-only gateway before enabling a scope."""
    if not _stable_library_id(library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    provider = getattr(request.app.state, "organization_cookie_provider", None)
    target_root = getattr(request.app.state, "organization_target_root_id", None)
    if provider is None or not target_root:
        raise HTTPException(status_code=503, detail="library_scope_unavailable")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None or library.root_directory_id != str(target_root):
            raise HTTPException(status_code=404, detail="library_not_found")
        if not library.scope_verified:
            gateway = P115ReadOnlyDirectoryGateway(
                provider,
                authorized_directory_ids=(library.root_directory_id,),
                request_timeout_seconds=30,
            )
            try:
                scan = await scan_directory(
                    gateway,
                    library.root_directory_id,
                    page_size=1,
                    max_pages=MAX_SCOPE_VERIFICATION_PAGES,
                    require_explicit_complete=True,
                )
            except (
                LibraryContractError,
                P115ReadOnlyGatewayError,
                TimeoutError,
                OSError,
            ):
                raise HTTPException(
                    status_code=503, detail="library_scope_verification_failed"
                ) from None
            if scan.state is not ScanState.COMPLETE or scan.scan_complete is not True:
                raise HTTPException(
                    status_code=503, detail="library_scope_verification_failed"
                )
            library.scope_verified = True
            library.enabled = True
            library.revision += 1
            await session.commit()
            await session.refresh(library)
        latest = await _latest_scans(session, {library.id})
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "library.scope.verified",
            fields={"status": "verified", "enabled": library.enabled},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="library",
            resource_id=library_id,
        )
    response = MediaLibraryResponse(
        library_id=library.id,
        name=library.name,
        root_directory_id=library.root_directory_id,
        enabled=library.enabled,
        scope_verified=library.scope_verified,
        revision=library.revision,
        latest_scan=_scan_summary(latest.get(library.id)),
    )
    return MediaLibraryVerificationResponse(
        library=response,
        verified=library.scope_verified,
        enabled=library.enabled,
    )


@router.post(
    "/libraries/{library_id}/scan",
    response_model=LibraryScanSummary,
)
async def scan_library(
    library_id: str,
    payload: LibraryScanRequest,
    request: Request,
    context: LibraryWriteDependency,
) -> LibraryScanSummary:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    if getattr(request.app.state, "organization_cookie_provider", None) is None:
        raise HTTPException(status_code=503, detail="library_scope_unavailable")
    try:
        summary = await _scan_operation_service(request).enqueue(
            library_id,
            idempotency_key=payload.idempotency_key,
            max_directories=payload.max_directories,
        )
    except LibraryScanOperationError as error:
        raise _scan_http_error(error) from None
    return _operation_summary(summary)


@router.get(
    "/libraries/{library_id}/scans", response_model=LibraryScanListResponse
)
async def list_library_scans(
    library_id: str,
    request: Request,
    context: LibraryReadDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LibraryScanListResponse:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    try:
        items, next_cursor = await _scan_operation_service(request).list(
            library_id, cursor=cursor, limit=limit
        )
    except LibraryScanOperationError as error:
        raise _scan_http_error(error) from None
    return LibraryScanListResponse(
        items=[_operation_summary(item) for item in items],
        next_cursor=next_cursor,
    )


@router.get(
    "/libraries/{library_id}/scans/{scan_run_id}", response_model=LibraryScanSummary
)
@router.get(
    "/libraries/{library_id}/scan/{scan_run_id}",
    response_model=LibraryScanSummary,
    include_in_schema=False,
)
async def get_library_scan(
    library_id: str,
    scan_run_id: str,
    request: Request,
    context: LibraryReadDependency,
) -> LibraryScanSummary:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    try:
        summary = await _scan_operation_service(request).get(library_id, scan_run_id)
    except LibraryScanOperationNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "library_scan_not_found", "message": "扫描操作不存在"},
        ) from None
    except LibraryScanOperationError as error:
        raise _scan_http_error(error) from None
    return _operation_summary(summary)


@router.post(
    "/libraries/{library_id}/scans/{scan_run_id}/cancel",
    response_model=LibraryScanSummary,
)
async def cancel_library_scan(
    library_id: str,
    scan_run_id: str,
    request: Request,
    context: LibraryWriteDependency,
) -> LibraryScanSummary:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    try:
        summary = await _scan_operation_service(request).cancel(
            library_id, scan_run_id
        )
    except LibraryScanOperationNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "library_scan_not_found", "message": "扫描操作不存在"},
        ) from None
    except LibraryScanOperationError as error:
        raise _scan_http_error(error) from None
    return _operation_summary(summary)


@router.get(
    "/libraries/{library_id}/scans/{scan_run_id}/entries",
    response_model=MediaEntryListResponse,
)
async def list_library_scan_entries(
    library_id: str,
    scan_run_id: str,
    request: Request,
    context: LibraryReadDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MediaEntryListResponse:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        run = await session.scalar(
            select(LibraryScanRun).where(
                LibraryScanRun.id == scan_run_id,
                LibraryScanRun.library_id == library_id,
            )
        )
        if run is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "library_scan_not_found", "message": "扫描操作不存在"},
            )
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry)
                    .where(
                        LibraryScanEntry.scan_run_id == scan_run_id,
                        LibraryScanEntry.is_directory.is_(False),
                    )
                    .order_by(LibraryScanEntry.object_id)
                    .offset(cursor)
                    .limit(limit + 1)
                )
            ).all()
        )
    has_more = len(entries) > limit
    entries = entries[:limit]
    return MediaEntryListResponse(
        items=[_entry_response(library_id, scan_run_id, entry) for entry in entries],
        next_cursor=cursor + limit if has_more else None,
    )


async def require_empty_directory_cleanup(request: Request) -> None:
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is None:
        raise HTTPException(status_code=503, detail="empty_directory_cleanup_unavailable")
    try:
        settings = await settings_service.get_organization()
    except Exception:  # noqa: BLE001 - settings detail stays private
        raise HTTPException(
            status_code=503, detail="empty_directory_cleanup_unavailable"
        ) from None
    if not settings.cleanup_empty_directories:
        raise HTTPException(status_code=503, detail="empty_directory_cleanup_disabled")


async def require_empty_directory_cleanup_apply(request: Request) -> None:
    await require_empty_directory_cleanup(request)
    ownership_service = getattr(
        request.app.state, "managed_directory_ownership_service", None
    )
    if ownership_service is None:
        raise HTTPException(status_code=503, detail="empty_directory_cleanup_unavailable")
    if not getattr(request.app.state, "organization_execution_enabled", False):
        raise HTTPException(status_code=503, detail="organization_execution_disabled")
    if not getattr(request.app.state, "organization_write_enabled", False):
        raise HTTPException(status_code=503, detail="organization_write_disabled")
    if not getattr(
        request.app.state, "organization_write_contract_verified", False
    ):
        raise HTTPException(status_code=503, detail="organization_write_unverified")


async def _empty_cleanup_settings(request: Request):
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is None:
        raise HTTPException(status_code=503, detail="empty_directory_cleanup_unavailable")
    try:
        return await settings_service.get_organization()
    except Exception:  # noqa: BLE001 - settings detail stays private
        raise HTTPException(
            status_code=503, detail="empty_directory_cleanup_unavailable"
        ) from None


def _empty_cleanup_protected_ids(library: MediaLibrary, settings) -> frozenset[str]:
    protected = set(settings.source_directory_ids)
    protected.add(library.root_directory_id)
    for directory_id in (settings.target_directory_id, settings.push_directory_id):
        if directory_id:
            protected.add(directory_id)
    return frozenset(protected)


def _empty_cleanup_error_status(code: str) -> int:
    return 404 if code == "plan_not_found" else 409


def _empty_cleanup_operation_scope(
    plan_id: str, payload: EmptyDirectoryCleanupPlanApplyRequest
) -> str:
    canonical = json.dumps(
        {
            "confirm": payload.confirm,
            "digest": payload.digest.lower(),
            "expected_revision": payload.expected_revision,
            "plan_id": plan_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "cleanup:" + hashlib.sha256(canonical).hexdigest()[:32]


async def _run_empty_cleanup_operation_heartbeat(
    operations: StrmOperationService,
    operation_id: str,
    lease_owner: str,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            try:
                await operations.heartbeat(operation_id, lease_owner=lease_owner)
            except StrmOperationError:
                return


def _start_empty_cleanup_operation_heartbeat(
    operations: StrmOperationService, operation_id: str, lease_owner: str
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(
        _run_empty_cleanup_operation_heartbeat(
            operations, operation_id, lease_owner, stop
        ),
        name=f"watch-assistant-empty-cleanup-heartbeat-{operation_id}",
    )
    return stop, task


async def _stop_empty_cleanup_operation_heartbeat(
    stop: asyncio.Event, task: asyncio.Task[None]
) -> None:
    stop.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@router.post(
    "/libraries/{library_id}/empty-directory-cleanup-plan",
    response_model=EmptyDirectoryCleanupPlanResponse,
    dependencies=[Depends(require_empty_directory_cleanup)],
)
async def create_empty_directory_cleanup_plan(
    library_id: str,
    payload: EmptyDirectoryCleanupPlanRequest,
    request: Request,
    context: LibraryWriteDependency,
) -> EmptyDirectoryCleanupPlanResponse:
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    settings = await _empty_cleanup_settings(request)
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library_not_found")
    try:
        plan = await EmptyDirectoryCleanupPlanService(
            request.app.state.database.session_factory
        ).create_plan(
            library_id=library_id,
            source_scan_run_id=payload.source_scan_run_id,
            protected_directory_ids=_empty_cleanup_protected_ids(library, settings),
        )
    except EmptyDirectoryCleanupPlanError as error:
        raise HTTPException(
            status_code=_empty_cleanup_error_status(error.code), detail=error.code
        ) from None
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "library.empty_directory_cleanup.plan.created",
            fields={"status": plan.status},
            counts={"count": plan.candidate_count},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="empty_directory_cleanup_plan",
            resource_id=plan.plan_id,
        )
    return EmptyDirectoryCleanupPlanResponse.model_validate(plan.to_public_dict())


@router.get(
    "/empty-directory-cleanup-plans/{plan_id}",
    response_model=EmptyDirectoryCleanupPlanResponse,
)
async def get_empty_directory_cleanup_plan(
    plan_id: str, request: Request, context: AuthDependency
) -> EmptyDirectoryCleanupPlanResponse:
    try:
        plan = await EmptyDirectoryCleanupPlanService(
            request.app.state.database.session_factory
        ).get_plan(plan_id)
    except EmptyDirectoryCleanupPlanError as error:
        raise HTTPException(
            status_code=_empty_cleanup_error_status(error.code), detail=error.code
        ) from None
    if context.via_bearer and context.library_ids and plan.library_id not in context.library_ids:
        raise HTTPException(status_code=404, detail="plan_not_found")
    return EmptyDirectoryCleanupPlanResponse.model_validate(plan.to_public_dict())


@router.post(
    "/empty-directory-cleanup-plans/{plan_id}/apply",
    response_model=EmptyDirectoryCleanupPlanApplyResponse,
    dependencies=[Depends(require_empty_directory_cleanup_apply)],
)
async def apply_empty_directory_cleanup_plan(
    plan_id: str,
    payload: EmptyDirectoryCleanupPlanApplyRequest,
    request: Request,
    context: OrganizeWriteDependency,
) -> EmptyDirectoryCleanupPlanApplyResponse:
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="confirmation_required")
    executor = getattr(request.app.state, "empty_directory_cleanup_executor", None)
    if not callable(executor):
        raise HTTPException(status_code=503, detail="empty_directory_cleanup_unavailable")
    service = EmptyDirectoryCleanupPlanService(
        request.app.state.database.session_factory
    )
    try:
        current_plan = await service.get_plan(plan_id)
    except EmptyDirectoryCleanupPlanError as error:
        raise HTTPException(
            status_code=_empty_cleanup_error_status(error.code), detail=error.code
        ) from None
    if (
        context.via_bearer
        and context.library_ids
        and current_plan.library_id not in context.library_ids
    ):
        raise HTTPException(status_code=404, detail="plan_not_found")
    operations = StrmOperationService(request.app.state.database.session_factory)
    try:
        queued = await operations.create(
            library_id=current_plan.library_id,
            source_scan_run_id=current_plan.source_scan_run_id,
            kind=StrmOperationKind.CLEANUP,
            workflow_id=_empty_cleanup_operation_scope(plan_id, payload),
            idempotency_key=payload.idempotency_key,
        )
        running, acquired = await operations.claim_start(queued.operation_id)
    except StrmOperationError as error:
        status = (
            409
            if error.code
            in {
                "idempotency_key_conflict",
                "strm_library_operation_conflict",
                "strm_operation_claim_conflict",
            }
            else 422
        )
        raise HTTPException(
            status_code=status, detail=error.code
        ) from None
    if not acquired:
        if running.status == "running":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "empty_cleanup_in_progress",
                    "message": "已复用正在执行的空目录清理操作，当前仍在执行",
                    "operation_id": running.operation_id,
                    "status": running.status,
                    "reused": True,
                },
            )
        if running.status == "succeeded":
            try:
                current_plan = await service.get_plan(plan_id)
            except EmptyDirectoryCleanupPlanError as error:
                raise HTTPException(
                    status_code=_empty_cleanup_error_status(error.code),
                    detail=error.code,
                ) from None
            if current_plan.status != "applied":
                raise HTTPException(
                    status_code=409, detail="empty_cleanup_uncertain"
                )
            return EmptyDirectoryCleanupPlanApplyResponse(
                plan=EmptyDirectoryCleanupPlanResponse.model_validate(
                    current_plan.to_public_dict()
                ),
                deleted=running.retired,
            )
        if running.status == "queued":
            raise HTTPException(
                status_code=409, detail="strm_library_operation_conflict"
            )
        raise HTTPException(
            status_code=409,
            detail=running.error_code or "empty_cleanup_failed",
        )

    lease_owner = await operations.get_lease_token(running.operation_id)
    if lease_owner is None:
        raise HTTPException(status_code=409, detail="strm_operation_lease_lost")
    heartbeat_stop, heartbeat_task = _start_empty_cleanup_operation_heartbeat(
        operations, running.operation_id, lease_owner
    )

    async def lease_check() -> bool:
        return await operations.is_lease_active(
            running.operation_id, lease_owner=lease_owner
        )

    try:
        try:
            result = await service.apply_plan(
                plan_id=plan_id,
                expected_revision=payload.expected_revision,
                digest=payload.digest,
                confirm=payload.confirm,
                idempotency_key=payload.idempotency_key,
                executor=executor,
                lease_check=lease_check,
                operation_id=running.operation_id,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    operations.cancel(
                        running.operation_id,
                        lease_owner=lease_owner,
                    )
                )
            except StrmOperationError:
                pass
            raise
        except EmptyDirectoryCleanupPlanError as error:
            try:
                await operations.fail(
                    running.operation_id,
                    error_code=error.code,
                    lease_owner=lease_owner,
                )
            except StrmOperationError as lifecycle_error:
                if lifecycle_error.code != "strm_operation_lease_lost":
                    raise
            raise HTTPException(
                status_code=_empty_cleanup_error_status(error.code),
                detail=error.code,
            ) from None
        except Exception:  # noqa: BLE001 - operation status must not remain running
            try:
                await operations.fail(
                    running.operation_id,
                    error_code="empty_cleanup_failed",
                    lease_owner=lease_owner,
                )
            except StrmOperationError:
                pass
            raise HTTPException(status_code=409, detail="empty_cleanup_failed") from None

        try:
            completed = await operations.complete(
                running.operation_id,
                generated=0,
                unchanged=0,
                skipped=0,
                failed=0,
                retired=result.deleted,
                lease_owner=lease_owner,
            )
        except StrmOperationError as error:
            if error.code == "strm_operation_lease_lost":
                raise HTTPException(
                    status_code=409, detail="empty_cleanup_uncertain"
                ) from None
            raise HTTPException(status_code=409, detail=error.code) from None
        if completed.status != "succeeded":
            raise HTTPException(status_code=409, detail="empty_cleanup_uncertain")
        settings_service = getattr(request.app.state, "settings_service", None)
        if settings_service is not None:
            await settings_service.log_event(
                "library.empty_directory_cleanup.applied",
                fields={"status": result.plan.status},
                counts={"count": result.deleted},
                actor_type="agent" if context.via_bearer else "web",
                actor_id=context.identity,
                resource_type="empty_directory_cleanup_plan",
                resource_id=result.plan.plan_id,
            )
        return EmptyDirectoryCleanupPlanApplyResponse(
            plan=EmptyDirectoryCleanupPlanResponse.model_validate(
                result.plan.to_public_dict()
            ),
            deleted=result.deleted,
        )
    finally:
        await _stop_empty_cleanup_operation_heartbeat(heartbeat_stop, heartbeat_task)


@router.post(
    "/libraries/{library_id}/organization-preview",
    response_model=OrganizationPlanResponse,
)
async def create_organization_preview(
    library_id: str,
    payload: OrganizationPreviewRequest,
    request: Request,
    context: LibraryWriteDependency,
) -> OrganizationPlanResponse:
    if not getattr(request.app.state, "organization_plan_enabled", False):
        raise HTTPException(status_code=503, detail="organization_plan_disabled")
    if not _stable_library_id(library_id) or not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    service = getattr(request.app.state, "organization_preview_service", None)
    if not isinstance(service, OrganizationPreviewService):
        raise HTTPException(status_code=503, detail="organization_preview_unavailable")
    settings_service = getattr(request.app.state, "settings_service", None)
    settings = await settings_service.get_organization() if settings_service is not None else None
    target_directories = None
    existing_target_files = ()
    if settings is not None and settings.target_directory_id is not None:
        provider = getattr(request.app.state, "organization_cookie_provider", None)
        if provider is None:
            raise HTTPException(status_code=503, detail="target_catalog_unavailable")
        try:
            catalog = await read_target_catalog(
                P115ReadOnlyDirectoryGateway(
                    provider,
                    authorized_directory_ids=(settings.target_directory_id,),
                    request_timeout_seconds=30,
                ),
                settings.target_directory_id,
            )
            target_directories = catalog.by_path
            existing_target_files = catalog.files
        except OrganizationTargetError as error:
            raise HTTPException(status_code=409, detail=error.code) from None
    try:
        plan = await service.create_preview(
            library_id=library_id,
            scan_run_id=payload.source_scan_run_id,
            source_directory_id=payload.source_directory_id,
            source_directory_ids=settings.source_directory_ids if settings else None,
            target_directory_id=settings.target_directory_id if settings else None,
            target_directories=target_directories,
            existing_target_files=existing_target_files,
            video_extensions=settings.video_extensions if settings else None,
            metadata_extensions=settings.metadata_extensions if settings else None,
            small_file_threshold_mb=settings.small_file_threshold_mb if settings else 0.0,
            rename_enabled=settings.rename_enabled if settings else True,
            region_grouping_enabled=settings.region_grouping_enabled if settings else True,
            year_grouping_enabled=settings.year_grouping_enabled if settings else False,
            include_children_category=settings.include_children_category if settings else False,
            include_concert_category=settings.include_concert_category if settings else False,
            media_probe_enabled=settings.media_probe_enabled if settings else False,
            ai_identification_enabled=settings.ai_identification_enabled if settings else False,
            cleanup_empty_directories=settings.cleanup_empty_directories if settings else False,
            strm_linkage_enabled=settings.strm_linkage_enabled if settings else False,
            prefer_remux=settings.prefer_remux if settings else True,
            prefer_resolution=settings.prefer_resolution if settings else True,
            prefer_dolby=settings.prefer_dolby if settings else False,
            conflict_mode=settings.conflict_mode if settings else 2,
            multi_version_enabled=settings.multi_version_enabled if settings else False,
        )
    except OrganizationPreviewError as error:
        raise HTTPException(
            status_code=error_status(error.code), detail=error.code
        ) from None
    except OrganizationPlanError as error:
        raise HTTPException(status_code=409, detail=error.code) from None
    if settings_service is not None:
        await settings_service.log_event(
            "organize.preview.created",
            fields={"status": plan.status.value},
            counts={"count": plan.source_count},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="organization_plan",
            resource_id=plan.plan_id,
        )
        if plan.status.value == "needs_review":
            await settings_service.log_event(
                "organize.needs_review",
                fields={"status": plan.status.value, "count": plan.source_count},
                actor_type="agent" if context.via_bearer else "web",
                actor_id=context.identity,
                resource_type="organization_plan",
                resource_id=plan.plan_id,
            )
    return OrganizationPlanResponse.model_validate(plan.to_public_dict())


@router.get("/libraries/{library_id}", response_model=MediaLibraryResponse)
async def get_library(library_id: str, request: Request, context: AuthDependency) -> MediaLibraryResponse:
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None or not _allowed(context, library_id):
            raise HTTPException(status_code=404, detail="library_not_found")
        latest = await _latest_scans(session, {library_id})
    return MediaLibraryResponse(
        library_id=library.id,
        name=library.name,
        root_directory_id=library.root_directory_id,
        enabled=library.enabled,
        scope_verified=library.scope_verified,
        revision=library.revision,
        latest_scan=_scan_summary(latest.get(library_id)),
    )


@router.get("/libraries/{library_id}/media", response_model=MediaEntryListResponse)
async def list_library_media(
    library_id: str,
    request: Request,
    context: AuthDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MediaEntryListResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        latest = (await _latest_scans(session, {library_id})).get(library_id)
        if latest is None:
            return MediaEntryListResponse(items=[], next_cursor=None)
        query = select(LibraryScanEntry).where(
            LibraryScanEntry.scan_run_id == latest.id,
            LibraryScanEntry.is_directory.is_(False),
        ).order_by(LibraryScanEntry.object_id)
        entries = list((await session.scalars(query.offset(cursor).limit(limit + 1))).all())
        has_more = len(entries) > limit
        entries = entries[:limit]
    return MediaEntryListResponse(
        items=[_entry_response(library_id, latest.id, entry) for entry in entries],
        next_cursor=cursor + limit if has_more else None,
    )


@router.post(
    "/libraries/{library_id}/objects/{object_id}/delete",
    response_model=LibraryDeleteResponse,
    dependencies=[Depends(require_permanent_delete_enabled)],
)
async def delete_library_object(
    library_id: str,
    object_id: str,
    payload: LibraryDeleteRequest,
    request: Request,
    context: OrganizeWriteDependency,
) -> LibraryDeleteResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    service = getattr(request.app.state, "p115_delete_service", None)
    if not isinstance(service, P115DeleteService):
        raise HTTPException(status_code=503, detail="delete_unavailable")
    result = await service.delete(
        library_id,
        object_id,
        expected_name=payload.expected_name,
        confirmed=payload.confirm,
        # 服务层门禁兜底:即使绕过路由依赖直接调用,删除前仍校验写开关。
        gate=lambda: _require_permanent_delete_enabled_state(request),
    )
    if result.status is not DeleteStatus.FAILED:
        settings_service = getattr(request.app.state, "settings_service", None)
        if settings_service is not None:
            await settings_service.log_event(
                "library.object.permanently_deleted",
                fields={"status": result.status.value},
                counts={"count": 1},
                actor_type="agent" if context.via_bearer else "web",
                actor_id=context.identity,
                resource_type="library",
                resource_id=library_id,
                error_code=result.error_code,
            )
    return LibraryDeleteResponse(status=result.status.value, error_code=result.error_code)


@router.get(
    "/libraries/{library_id}/inventory/events",
    response_model=InventoryEventListResponse,
)
async def list_inventory_events(
    library_id: str,
    request: Request,
    context: AuthDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> InventoryEventListResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        rows = list(
            (
                await session.scalars(
                    select(LibraryInventoryEvent)
                    .where(LibraryInventoryEvent.library_id == library_id)
                    .order_by(
                        LibraryInventoryEvent.created_at.desc(),
                        LibraryInventoryEvent.id.desc(),
                    )
                    .offset(cursor)
                    .limit(limit + 1)
                )
            ).all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return InventoryEventListResponse(
        items=[
            InventoryEventResponse(
                event_id=row.id,
                library_id=row.library_id,
                scan_run_id=row.scan_run_id,
                object_type=row.object_type,
                object_id=row.object_id,
                event_kind=row.event_kind,
                previous_status=row.previous_status,
                created_at=row.created_at,
            )
            for row in rows
        ],
        next_cursor=cursor + limit if has_more else None,
    )


@router.get("/media", response_model=MediaEntryListResponse)
async def list_media(
    request: Request,
    context: AuthDependency,
    library_id: Annotated[str | None, Query(alias="library")] = None,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MediaEntryListResponse:
    if library_id is not None and not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    allowed_ids = set(context.library_ids) if context.via_bearer and context.library_ids else None
    if library_id is not None:
        allowed_ids = {library_id}
    async with request.app.state.database.session_factory() as session:
        latest = await _latest_scans(session, allowed_ids)
        if not latest:
            return MediaEntryListResponse(items=[], next_cursor=None)
        query = select(LibraryScanEntry).where(
            LibraryScanEntry.scan_run_id.in_([run.id for run in latest.values()]),
            LibraryScanEntry.is_directory.is_(False),
        ).order_by(LibraryScanEntry.object_id)
        entries = list((await session.scalars(query.offset(cursor).limit(limit + 1))).all())
        has_more = len(entries) > limit
        entries = entries[:limit]
        run_to_library = {run.id: library for library, run in latest.items()}
    return MediaEntryListResponse(
        items=[_entry_response(run_to_library[entry.scan_run_id], entry.scan_run_id, entry) for entry in entries],
        next_cursor=cursor + limit if has_more else None,
    )


@router.get("/media/{media_id}", response_model=MediaEntryResponse)
async def get_media(media_id: str, request: Request, context: AuthDependency) -> MediaEntryResponse:
    if ":" in media_id:
        object_type, object_id = media_id.split(":", 1)
    else:
        object_type, object_id = "file", media_id
    if object_type != "file" or not object_id or len(object_id) > 128:
        raise HTTPException(status_code=404, detail="media_not_found")
    allowed_ids = set(context.library_ids) if context.via_bearer and context.library_ids else None
    async with request.app.state.database.session_factory() as session:
        latest = await _latest_scans(session, allowed_ids)
        if not latest:
            raise HTTPException(status_code=404, detail="media_not_found")
        query = select(LibraryScanEntry).where(
            LibraryScanEntry.scan_run_id.in_([run.id for run in latest.values()]),
            LibraryScanEntry.object_type == "file",
            LibraryScanEntry.object_id == object_id,
            LibraryScanEntry.is_directory.is_(False),
        )
        entry = await session.scalar(query)
        if entry is None:
            raise HTTPException(status_code=404, detail="media_not_found")
        library_id = next((library for library, run in latest.items() if run.id == entry.scan_run_id), None)
        if library_id is None:
            raise HTTPException(status_code=404, detail="media_not_found")
    return _entry_response(library_id, entry.scan_run_id, entry)


@router.get("/libraries/{library_id}/inventory", response_model=LibraryInventoryResponse)
async def get_library_inventory(
    library_id: str,
    request: Request,
    context: AuthDependency,
    duplicate_limit: Annotated[int, Query(ge=0, le=100)] = 20,
) -> LibraryInventoryResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        run = await _latest_scan_any(session, library_id)
        snapshot = await _inventory_snapshot(session, run, library=library)
    return _inventory_response(
        library_id,
        run,
        snapshot,
        duplicate_limit=duplicate_limit,
    )


@router.get(
    "/libraries/{library_id}/inventory/check",
    response_model=InventoryCheckResponse,
)
async def check_library_inventory(
    library_id: str,
    request: Request,
    context: AuthDependency,
    object_id: Annotated[str | None, Query(max_length=128)] = None,
    content_digest: Annotated[str | None, Query(max_length=256)] = None,
    infohash: Annotated[str | None, Query(max_length=128)] = None,
    tmdb_id: Annotated[int | None, Query(gt=0)] = None,
    media_type: Annotated[Literal["movie", "tv"] | None, Query()] = None,
    season: Annotated[int | None, Query(ge=0)] = None,
    episode_start: Annotated[int | None, Query(ge=1)] = None,
    episode_end: Annotated[int | None, Query(ge=1)] = None,
    name: Annotated[str | None, Query(max_length=512)] = None,
) -> InventoryCheckResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        run = await _latest_scan_any(session, library_id)
        snapshot = await _inventory_snapshot(session, run, library=library)
    decision = check_inventory(
        snapshot,
        object_id=object_id,
        content_digest=content_digest,
        infohash=infohash,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        name=name,
    )
    return InventoryCheckResponse(
        library_id=library_id,
        scan_run_id=None if run is None else run.id,
        freshness=_freshness_response(snapshot),
        decision=decision.value,
        matched_object_count=_matched_count(
            snapshot,
            object_id=object_id,
            content_digest=content_digest,
            infohash=infohash,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode_start=episode_start,
            episode_end=episode_end,
        ),
    )


@router.put(
    "/libraries/{library_id}/inventory/identities/{object_id}",
    response_model=InventoryIdentityResponse,
)
async def bind_library_identity(
    library_id: str,
    object_id: str,
    payload: InventoryIdentityPatch,
    request: Request,
    context: ReviewWriteDependency,
) -> InventoryIdentityResponse:
    if not _allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found")
    async with request.app.state.database.session_factory() as session:
        library = await session.get(MediaLibrary, library_id)
        if library is None:
            raise HTTPException(status_code=404, detail="library_not_found")
        run = await _latest_scan_any(session, library_id)
        if not await scan_has_verified_evidence(session, run, library):
            raise HTTPException(status_code=409, detail="library_inventory_incomplete")
        captured_at = run.updated_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        freshness = calculate_freshness(complete=True, captured_at=captured_at)
        if freshness.status is not FreshnessStatus.FRESH:
            raise HTTPException(status_code=409, detail="library_inventory_incomplete")
        entry = await session.scalar(
            select(LibraryScanEntry).where(
                LibraryScanEntry.scan_run_id == run.id,
                LibraryScanEntry.object_type == "file",
                LibraryScanEntry.object_id == object_id,
                LibraryScanEntry.is_directory.is_(False),
            )
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="media_not_found")
        identity = await session.scalar(
            select(LibraryMediaIdentity).where(
                LibraryMediaIdentity.library_id == library_id,
                LibraryMediaIdentity.object_id == object_id,
            )
        )
        if identity is None:
            if payload.revision != 0:
                raise HTTPException(status_code=409, detail="library_identity_conflict")
            identity = LibraryMediaIdentity(
                id=_identity_record_id(library_id, object_id),
                library_id=library_id,
                object_id=object_id,
                tmdb_id=payload.tmdb_id,
                media_type=payload.media_type,
                season=payload.season,
                episode_start=payload.episode_start,
                episode_end=payload.episode_end,
                confidence="trusted",
                revision=1,
            )
            session.add(identity)
        else:
            if identity.revision != payload.revision:
                raise HTTPException(status_code=409, detail="library_identity_conflict")
            identity.tmdb_id = payload.tmdb_id
            identity.media_type = payload.media_type
            identity.season = payload.season
            identity.episode_start = payload.episode_start
            identity.episode_end = payload.episode_end
            identity.confidence = "trusted"
            identity.revision += 1
        await session.commit()
        await session.refresh(identity)
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "library.identity.bound",
            fields={"status": "saved", "media_type": payload.media_type},
            actor_type="agent" if context.via_bearer else "web",
            resource_type="library",
            resource_id=library_id,
        )
    return InventoryIdentityResponse(
        library_id=identity.library_id,
        object_id=identity.object_id,
        tmdb_id=identity.tmdb_id,
        media_type=identity.media_type,
        season=identity.season,
        episode_start=identity.episode_start,
        episode_end=identity.episode_end,
        confidence=identity.confidence,
        revision=identity.revision,
    )


def _entry_response(library_id: str, run_id: str, entry: LibraryScanEntry) -> MediaEntryResponse:
    return MediaEntryResponse(
        media_id=_media_id(entry.object_type, entry.object_id),
        library_id=library_id,
        scan_run_id=run_id,
        object_type="file",
        object_id=entry.object_id,
        parent_id=entry.parent_id,
        name=entry.name,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
    )


async def _latest_scan_any(session, library_id: str) -> LibraryScanRun | None:
    library = await session.get(MediaLibrary, library_id)
    return await verified_latest_scan(session, library)


async def _inventory_snapshot(
    session,
    run: LibraryScanRun | None,
    *,
    library: MediaLibrary | None = None,
) -> InventorySnapshot:
    if run is None:
        return build_snapshot((), complete=False, captured_at=None)
    entries = list(
        (
            await session.scalars(
                select(LibraryScanEntry)
                .where(
                    LibraryScanEntry.scan_run_id == run.id,
                    LibraryScanEntry.is_directory.is_(False),
                )
                .order_by(LibraryScanEntry.object_id)
            )
        ).all()
    )
    complete = await scan_has_verified_evidence(session, run, library)
    identities = {
        identity.object_id: identity
        for identity in (
            await session.scalars(
                select(LibraryMediaIdentity).where(
                    LibraryMediaIdentity.library_id == run.library_id
                )
            )
        ).all()
    }
    return build_snapshot(
        (
            InventoryFile(
                object_id=entry.object_id,
                name=entry.name,
                size_bytes=entry.size_bytes,
                modified_at=entry.modified_at,
                tmdb_id=(
                    identities[entry.object_id].tmdb_id
                    if entry.object_id in identities
                    else None
                ),
                media_type=(
                    identities[entry.object_id].media_type
                    if entry.object_id in identities
                    else None
                ),
                season=(
                    identities[entry.object_id].season
                    if entry.object_id in identities
                    else None
                ),
                episode_start=(
                    identities[entry.object_id].episode_start
                    if entry.object_id in identities
                    else None
                ),
                episode_end=(
                    identities[entry.object_id].episode_end
                    if entry.object_id in identities
                    else None
                ),
            )
            for entry in entries
        ),
        complete=complete,
        captured_at=(
            run.updated_at
            if run.updated_at.tzinfo is not None
            else run.updated_at.replace(tzinfo=UTC)
        ),
    )


def _scan_is_complete(
    run: LibraryScanRun | None,
    library: MediaLibrary | None = None,
) -> bool:
    """Retain the lightweight display predicate for existing callers.

    Mutating identity writes and inventory conclusions use the async evidence
    verifier above; this helper only answers the model-level completeness
    question used by compatibility tests and response shaping.
    """

    return bool(
        run is not None
        and run.complete
        and run.state == "completed"
        and run.snapshot_revision is not None
        and (
            library is None
            or (
                library.enabled
                and library.scope_verified
                and run.library_id == library.id
                and run.root_directory_id == library.root_directory_id
            )
        )
    )


def _identity_record_id(library_id: str, object_id: str) -> str:
    return hashlib.sha256(f"{library_id}:{object_id}".encode()).hexdigest()


def _freshness_response(snapshot: InventorySnapshot) -> InventoryFreshnessResponse:
    freshness = snapshot.freshness
    return InventoryFreshnessResponse(
        complete=freshness.complete,
        captured_at=freshness.captured_at,
        age_seconds=freshness.age_seconds,
        threshold_seconds=freshness.threshold_seconds,
        status=freshness.status.value,
    )


def _inventory_response(
    library_id: str,
    run: LibraryScanRun | None,
    snapshot: InventorySnapshot,
    *,
    duplicate_limit: int,
) -> LibraryInventoryResponse:
    groups = snapshot.duplicate_groups[:duplicate_limit]
    movie_count = sum(
        item.identity.media_type == "movie" for item in snapshot.files
    )
    tv_count = sum(item.identity.media_type == "tv" for item in snapshot.files)
    return LibraryInventoryResponse(
        library_id=library_id,
        scan_run_id=None if run is None else run.id,
        snapshot_revision=None if run is None else run.snapshot_revision,
        freshness=_freshness_response(snapshot),
        file_count=len(snapshot.files),
        movie_candidate_count=movie_count,
        tv_candidate_count=tv_count,
        unknown_count=len(snapshot.files) - movie_count - tv_count,
        duplicate_group_count=len(snapshot.duplicate_groups),
        duplicate_groups=[
            InventoryDuplicateGroupResponse(
                group_key=group.group_key,
                kind=group.kind.value,
                identity_key=group.identity_key,
                object_ids=list(group.object_ids),
                confidence=group.confidence,
            )
            for group in groups
        ],
    )


def _matched_count(
    snapshot: InventorySnapshot,
    *,
    object_id: str | None,
    content_digest: str | None,
    infohash: str | None,
    tmdb_id: int | None,
    media_type: Literal["movie", "tv"] | None,
    season: int | None,
    episode_start: int | None,
    episode_end: int | None,
) -> int:
    if not snapshot.complete:
        return 0
    return sum(
        (
            (object_id is not None and item.file.object_id == object_id)
            or (
                content_digest is not None
                and item.file.content_digest == content_digest
            )
            or (infohash is not None and item.file.infohash == infohash)
            or (
                tmdb_id is not None
                and item.identity.tmdb_id == tmdb_id
                and (media_type is None or item.identity.media_type == media_type)
                and (
                    media_type != "tv"
                    or season is None
                    or item.identity.season == season
                )
                and (
                    episode_start is None
                    or (
                        item.identity.episode_start is not None
                        and item.identity.episode_end is not None
                        and item.identity.episode_start <= (episode_end or episode_start)
                        and item.identity.episode_end >= episode_start
                    )
                )
            )
        )
        for item in snapshot.files
    )


__all__ = ["router"]
