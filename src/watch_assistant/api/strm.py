"""Authenticated STRM manifest, generation, and playback routes."""

import asyncio
import ipaddress
import math
from collections.abc import Collection
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from watch_assistant.adapters.p115_playback_contract import (
    PlaybackContractError,
    PlaybackGate,
    PlaybackStatus,
    make_playback_request,
)
from watch_assistant.schemas import (
    StrmCleanupPlanApplyRequest,
    StrmCleanupPlanApplyResponse,
    StrmCleanupPlanRequest,
    StrmCleanupPlanResponse,
    StrmGenerationRequest,
    StrmGenerationResponse,
    StrmManifestItemResponse,
    StrmManifestListResponse,
    StrmOperationListResponse,
    StrmOperationResponse,
    StrmVerifyRequest,
    StrmVerifyResponse,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.security import AuthContext, require_api_auth, require_scope
from watch_assistant.services.strm_cleanup_plan import (
    StrmCleanupPlanError,
    StrmCleanupPlanService,
)
from watch_assistant.services.strm_manifest import (
    StrmGenerationSummary,
    StrmManifestError,
    StrmManifestService,
)
from watch_assistant.services.strm_operations import (
    StrmOperationError,
    StrmOperationKind,
    StrmOperationNotFound,
    StrmOperationService,
    StrmOperationSummary,
)
from watch_assistant.services.strm_verification import (
    StrmVerificationError,
    StrmVerificationService,
)
from watch_assistant.services.workflows import (
    WorkflowNotFound,
    sync_child_stage_in_transaction,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


async def require_strm_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_full_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_full_disabled", "message": "STRM 功能未启用"},
        )


async def require_strm_incremental_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_incremental_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strm_incremental_disabled",
                "message": "STRM 增量同步未启用",
            },
        )


async def require_strm_cleanup_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_cleanup_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_cleanup_disabled", "message": "STRM 失效清理未启用"},
        )


async def require_strm_playback_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_playback_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_playback_disabled", "message": "STRM 播放未启用"},
        )
    if not getattr(request.app.state, "strm_playback_contract_verified", False):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strm_playback_unverified",
                "message": "STRM 播放契约尚未验收",
            },
        )
    if not getattr(request.app.state, "strm_playback_supported", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_playback_unavailable", "message": "STRM 播放暂不可用"},
        )
    _require_playback_network(request)


def _service(request: Request) -> StrmManifestService:
    service = getattr(request.app.state, "strm_manifest_service", None)
    if not isinstance(service, StrmManifestService):
        raise HTTPException(status_code=503, detail="strm_unavailable")
    return service


ServiceDependency = Annotated[StrmManifestService, Depends(_service)]


async def _sync_workflow_stage(
    request: Request,
    workflow_id: str | None,
    *,
    operation_id: str,
    status: WorkflowStageStatus,
    reason: str,
    error_code: str | None = None,
) -> None:
    if workflow_id is None:
        return
    try:
        await sync_child_stage_in_transaction(
            request.app.state.database.session_factory,
            workflow_id,
            WorkflowStageName.STRM,
            child_type="strm_operation",
            child_id=operation_id,
            status=status,
            reason=reason,
            error_code=error_code,
            event_logger=getattr(request.app.state, "settings_service", None),
        )
    except WorkflowNotFound:
        raise HTTPException(status_code=404, detail="workflow_not_found") from None


def _operation_service(request: Request) -> StrmOperationService:
    return StrmOperationService(request.app.state.database.session_factory)


def _operation_response(summary: StrmOperationSummary) -> StrmOperationResponse:
    return StrmOperationResponse(
        operation_id=summary.operation_id,
        library_id=summary.library_id,
        source_scan_run_id=summary.source_scan_run_id,
        workflow_id=summary.workflow_id,
        kind=summary.kind,
        status=summary.status,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
        error_code=summary.error_code,
        created_at=summary.created_at,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
    )


async def _operation_cancelled(
    operations: StrmOperationService, operation_id: str
) -> bool:
    try:
        return await operations.is_cancelled(operation_id)
    except StrmOperationNotFound:
        return True


async def _operation_progress(
    operations: StrmOperationService,
    operation_id: str,
    summary: StrmGenerationSummary,
) -> None:
    await operations.progress(
        operation_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


def _library_allowed(context: AuthContext, library_id: str) -> bool:
    return (
        not context.via_bearer
        or not context.library_ids
        or library_id in context.library_ids
    )


async def _begin_operation(
    request: Request,
    *,
    library_id: str,
    payload: StrmGenerationRequest,
    kind: StrmOperationKind,
) -> tuple[StrmOperationService, StrmOperationSummary]:
    operations = _operation_service(request)
    queued = await operations.create(
        library_id=library_id,
        source_scan_run_id=payload.source_scan_run_id,
        kind=kind,
        workflow_id=payload.workflow_id,
    )
    try:
        running = await operations.start(queued.operation_id)
        await _sync_workflow_stage(
            request,
            payload.workflow_id,
            operation_id=running.operation_id,
            status=WorkflowStageStatus.RUNNING,
            reason="strm_started",
        )
    except asyncio.CancelledError:
        await _cancel_operation(
            request,
            operations,
            queued.operation_id,
            workflow_id=payload.workflow_id,
        )
        raise
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, str) else "workflow_not_found"
        await operations.fail(queued.operation_id, error_code=detail)
        raise
    return operations, running


async def _finish_operation(
    request: Request,
    operations: StrmOperationService,
    operation_id: str,
    *,
    workflow_id: str | None,
    kind: StrmOperationKind,
    summary: StrmGenerationSummary,
) -> StrmOperationSummary:
    if summary.failed:
        operation = await operations.fail(
            operation_id,
            error_code=f"strm_{kind.value}_failed",
            generated=summary.generated,
            unchanged=summary.unchanged,
            skipped=summary.skipped,
            failed=summary.failed,
            retired=summary.retired,
        )
        status = WorkflowStageStatus.FAILED
        error_code = operation.error_code
    else:
        operation = await operations.complete(
            operation_id,
            generated=summary.generated,
            unchanged=summary.unchanged,
            skipped=summary.skipped,
            failed=summary.failed,
            retired=summary.retired,
        )
        status = WorkflowStageStatus.SUCCEEDED
        error_code = None
    await _sync_workflow_stage(
        request,
        workflow_id,
        operation_id=operation_id,
        status=status,
        reason="strm_finished",
        error_code=error_code,
    )
    return operation


async def _execute_manifest_operation(
    request: Request,
    service: StrmManifestService,
    operations: StrmOperationService,
    running: StrmOperationSummary,
    *,
    kind: StrmOperationKind,
    propagate_errors: bool = True,
) -> tuple[StrmGenerationSummary | None, StrmOperationSummary]:
    heartbeat_stop, heartbeat_task = _start_operation_heartbeat(
        operations, running.operation_id
    )
    try:
        output_root = Path(
            getattr(request.app.state, "strm_output_root", "./data/strm")
        )
        playback_url_prefix = getattr(
            request.app.state,
            "strm_playback_url_prefix",
            "http://127.0.0.1:8115/api/v1/strm/play",
        )
        async def cancel_check() -> bool:
            return await _operation_cancelled(operations, running.operation_id)

        async def progress_callback(progress: StrmGenerationSummary) -> None:
            await _operation_progress(operations, running.operation_id, progress)

        if kind is StrmOperationKind.FULL:
            summary = await service.generate(
                running.library_id,
                source_scan_run_id=running.source_scan_run_id,
                output_root=output_root,
                playback_url_prefix=playback_url_prefix,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
        elif kind is StrmOperationKind.INCREMENTAL:
            summary = await service.incremental(
                running.library_id,
                source_scan_run_id=running.source_scan_run_id,
                output_root=output_root,
                playback_url_prefix=playback_url_prefix,
                retire_removed=False,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
        else:
            raise StrmManifestError("strm_operation_not_resumable")
        operation = await _finish_operation(
            request,
            operations,
            running.operation_id,
            workflow_id=running.workflow_id,
            kind=kind,
            summary=summary,
        )
        return summary, operation
    except asyncio.CancelledError:
        await _cancel_operation(
            request,
            operations,
            running.operation_id,
            workflow_id=running.workflow_id,
        )
        raise
    except StrmManifestError as error:
        operation = await _fail_operation(
            request,
            operations,
            running.operation_id,
            workflow_id=running.workflow_id,
            error_code=str(error),
        )
        if propagate_errors:
            raise
        return None, operation
    except Exception:  # noqa: BLE001 - operation status must not remain running
        operation = await _fail_operation(
            request,
            operations,
            running.operation_id,
            workflow_id=running.workflow_id,
            error_code="strm_operation_failed",
        )
        if propagate_errors:
            raise
        return None, operation
    finally:
        await _stop_operation_heartbeat(heartbeat_stop, heartbeat_task)


async def _fail_operation(
    request: Request,
    operations: StrmOperationService,
    operation_id: str,
    *,
    workflow_id: str | None,
    error_code: str,
) -> StrmOperationSummary:
    operation = await operations.fail(operation_id, error_code=error_code)
    if operation.status == "failed":
        await _sync_workflow_stage(
            request,
            workflow_id,
            operation_id=operation_id,
            status=WorkflowStageStatus.FAILED,
            reason="strm_failed",
            error_code=error_code,
        )
    return operation


async def _cancel_operation(
    request: Request,
    operations: StrmOperationService,
    operation_id: str,
    *,
    workflow_id: str | None,
) -> None:
    """Persist cancellation in an independent task before returning."""

    cleanup_task = asyncio.create_task(
        _cancel_operation_state(
            request, operations, operation_id, workflow_id=workflow_id
        ),
        name=f"watch-assistant-strm-cancel-{operation_id}",
    )
    # The request task may receive more than one cancellation while the
    # ledger transaction is committing. Keep shielding the independent task
    # until its result is observed, then let the caller re-raise cancellation.
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001 - observe task failure below
            break
    try:
        cleanup_task.result()
    except BaseException:  # noqa: BLE001 - preserve the original cancellation
        # A process stop can still interrupt the event loop itself. Startup
        # recovery handles that case; the original request cancellation wins.
        return


async def _cancel_operation_state(
    request: Request | None,
    operations: StrmOperationService,
    operation_id: str,
    *,
    workflow_id: str | None,
) -> StrmOperationSummary:
    cancel = getattr(operations, "cancel", None)
    if callable(cancel):
        operation = await cancel(operation_id)
        if operation.status == "cancelled" and request is not None:
            await _sync_workflow_stage(
                request,
                workflow_id,
                operation_id=operation_id,
                status=WorkflowStageStatus.FAILED,
                reason="strm_cancelled",
                error_code=operation.error_code,
            )
        return operation
    return await _fail_operation(
        request,
        operations,
        operation_id,
        workflow_id=workflow_id,
        error_code="strm_operation_cancelled",
    )


async def _run_operation_heartbeat(
    operations: StrmOperationService,
    operation_id: str,
    stop: asyncio.Event,
) -> None:
    """Renew the local lease while a manifest operation is doing I/O."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            try:
                await operations.heartbeat(operation_id)
            except (StrmOperationNotFound, StrmOperationError):
                return


def _start_operation_heartbeat(
    operations: StrmOperationService, operation_id: str
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(
        _run_operation_heartbeat(operations, operation_id, stop),
        name=f"watch-assistant-strm-heartbeat-{operation_id}",
    )
    return stop, task


async def _stop_operation_heartbeat(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@router.get(
    "/libraries/{library_id}/strm-manifest",
    response_model=StrmManifestListResponse,
    dependencies=[Depends(require_strm_enabled)],
)
async def list_manifest(
    library_id: str,
    service: ServiceDependency,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> StrmManifestListResponse:
    try:
        items, total = await service.list_current(
            library_id, page=page, page_size=page_size
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmManifestListResponse(
        items=[
            StrmManifestItemResponse(
                manifest_id=item.manifest_id,
                library_id=item.library_id,
                cloud_file_id=item.cloud_file_id,
                cloud_relative_path=item.cloud_relative_path,
                local_relative_path=item.local_relative_path,
                status=item.status,
                source_version=item.source_version,
            )
            for item in items
        ],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get(
    "/strm-operations/{operation_id}",
    response_model=StrmOperationResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:read")),
    ],
)
async def get_strm_operation(
    operation_id: str, request: Request, context: AuthDependency
) -> StrmOperationResponse:
    try:
        summary = await _operation_service(request).get(operation_id)
    except StrmOperationNotFound:
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    except StrmOperationError as error:
        raise HTTPException(status_code=422, detail=error.code) from None
    if not _library_allowed(context, summary.library_id):
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    return _operation_response(summary)


@router.post(
    "/strm-operations/{operation_id}/cancel",
    response_model=StrmOperationResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def cancel_strm_operation(
    operation_id: str, request: Request, context: AuthDependency
) -> StrmOperationResponse:
    operations = _operation_service(request)
    try:
        current = await operations.get(operation_id)
    except StrmOperationNotFound:
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    except StrmOperationError as error:
        raise HTTPException(status_code=422, detail=error.code) from None
    if not _library_allowed(context, current.library_id):
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    try:
        summary = await operations.cancel(operation_id)
    except StrmOperationNotFound:
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    return _operation_response(summary)


@router.post(
    "/strm-operations/{operation_id}/resume",
    response_model=StrmOperationResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def resume_strm_operation(
    operation_id: str,
    request: Request,
    context: AuthDependency,
    service: ServiceDependency,
) -> StrmOperationResponse:
    operations = _operation_service(request)
    try:
        current = await operations.get(operation_id)
    except StrmOperationNotFound:
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    except StrmOperationError as error:
        raise HTTPException(status_code=422, detail=error.code) from None
    if not _library_allowed(context, current.library_id):
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    if current.kind == StrmOperationKind.CLEANUP.value:
        raise HTTPException(status_code=409, detail="strm_operation_not_resumable")
    if (
        current.kind == StrmOperationKind.INCREMENTAL.value
        and not getattr(request.app.state, "strm_incremental_enabled", False)
    ):
        raise HTTPException(status_code=503, detail="strm_incremental_disabled")
    try:
        resumed = await operations.resume(operation_id)
    except StrmOperationNotFound:
        raise HTTPException(status_code=404, detail="strm_operation_not_found") from None
    if resumed.status != "queued":
        return _operation_response(resumed)
    try:
        running = await operations.start(operation_id)
        await _sync_workflow_stage(
            request,
            running.workflow_id,
            operation_id=running.operation_id,
            status=WorkflowStageStatus.RUNNING,
            reason="strm_resumed",
        )
    except asyncio.CancelledError:
        await _cancel_operation(
            request,
            operations,
            operation_id,
            workflow_id=resumed.workflow_id,
        )
        raise
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, str) else "workflow_not_found"
        await operations.fail(operation_id, error_code=detail)
        raise
    _, terminal = await _execute_manifest_operation(
        request,
        service,
        operations,
        running,
        kind=StrmOperationKind(resumed.kind),
        propagate_errors=False,
    )
    return _operation_response(terminal)


@router.get(
    "/libraries/{library_id}/strm-operations",
    response_model=StrmOperationListResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:read")),
    ],
)
async def list_strm_operations(
    library_id: str,
    request: Request,
    context: AuthDependency,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> StrmOperationListResponse:
    if not _library_allowed(context, library_id):
        raise HTTPException(status_code=404, detail="library_not_found") from None
    try:
        items, next_cursor = await _operation_service(request).list(
            library_id, cursor=cursor, limit=limit
        )
    except StrmOperationError as error:
        raise HTTPException(status_code=422, detail=error.code) from None
    return StrmOperationListResponse(
        items=[_operation_response(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post(
    "/libraries/{library_id}/strm-generation",
    response_model=StrmGenerationResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def generate_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    service: ServiceDependency,
    request: Request,
) -> StrmGenerationResponse:
    operations, running = await _begin_operation(
        request,
        library_id=library_id,
        payload=payload,
        kind=StrmOperationKind.FULL,
    )
    try:
        summary, operation = await _execute_manifest_operation(
            request,
            service,
            operations,
            running,
            kind=StrmOperationKind.FULL,
        )
    except asyncio.CancelledError:
        raise
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except Exception:  # noqa: BLE001 - operation status must not remain running
        raise HTTPException(status_code=409, detail="strm_operation_failed") from None
    if summary is None:
        raise HTTPException(status_code=409, detail="strm_operation_failed")
    return StrmGenerationResponse(
        operation_id=operation.operation_id,
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


@router.post(
    "/libraries/{library_id}/strm-incremental",
    response_model=StrmGenerationResponse,
    dependencies=[
        Depends(require_strm_incremental_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def incremental_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    service: ServiceDependency,
    request: Request,
) -> StrmGenerationResponse:
    operations, running = await _begin_operation(
        request,
        library_id=library_id,
        payload=payload,
        kind=StrmOperationKind.INCREMENTAL,
    )
    try:
        summary, operation = await _execute_manifest_operation(
            request,
            service,
            operations,
            running,
            kind=StrmOperationKind.INCREMENTAL,
        )
    except asyncio.CancelledError:
        raise
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except Exception:  # noqa: BLE001 - operation status must not remain running
        raise HTTPException(status_code=409, detail="strm_operation_failed") from None
    if summary is None:
        raise HTTPException(status_code=409, detail="strm_operation_failed")
    return StrmGenerationResponse(
        operation_id=operation.operation_id,
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


@router.post(
    "/libraries/{library_id}/strm-cleanup-plan",
    response_model=StrmCleanupPlanResponse,
    dependencies=[Depends(require_strm_enabled)],
)
async def create_cleanup_plan(
    library_id: str,
    payload: StrmCleanupPlanRequest,
    request: Request,
    context: AuthDependency,
) -> StrmCleanupPlanResponse:
    service = StrmCleanupPlanService(request.app.state.database.session_factory)
    try:
        plan = await service.create_plan(
            library_id=library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=getattr(request.app.state, "strm_output_root", "./data/strm"),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmCleanupPlanError as error:
        raise HTTPException(status_code=409, detail=error.code) from None
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "strm.cleanup.plan.created",
            fields={"status": plan.status},
            counts={"count": plan.candidate_count},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="strm_cleanup_plan",
            resource_id=plan.plan_id,
        )
    return StrmCleanupPlanResponse.model_validate(plan.to_public_dict())


@router.post(
    "/strm-cleanup-plans/{plan_id}/apply",
    response_model=StrmCleanupPlanApplyResponse,
    dependencies=[
        Depends(require_strm_cleanup_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def apply_cleanup_plan(
    plan_id: str,
    payload: StrmCleanupPlanApplyRequest,
    request: Request,
    context: AuthDependency,
) -> StrmCleanupPlanApplyResponse:
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="confirmation_required")
    service = StrmCleanupPlanService(request.app.state.database.session_factory)
    try:
        result = await service.apply_plan(
            plan_id=plan_id,
            expected_revision=payload.expected_revision,
            digest=payload.digest,
            confirm=payload.confirm,
            idempotency_key=payload.idempotency_key,
            output_root=getattr(request.app.state, "strm_output_root", "./data/strm"),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmCleanupPlanError as error:
        statuses = {
            "plan_not_found": 404,
            "cleanup_plan_expired": 409,
            "cleanup_plan_blocked": 409,
            "cleanup_plan_changed": 409,
            "cleanup_plan_not_reviewable": 409,
            "cleanup_plan_already_applied": 409,
            "plan_revision_changed": 409,
            "plan_digest_mismatch": 409,
        }
        raise HTTPException(
            status_code=statuses.get(error.code, 409), detail=error.code
        ) from None
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "strm.cleanup.applied",
            fields={"status": result.plan.status},
            counts={"count": result.retired},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="strm_cleanup_plan",
            resource_id=result.plan.plan_id,
        )
    return StrmCleanupPlanApplyResponse(
        plan=StrmCleanupPlanResponse.model_validate(result.plan.to_public_dict()),
        retired=result.retired,
    )


@router.get(
    "/strm-cleanup-plans/{plan_id}",
    response_model=StrmCleanupPlanResponse,
    dependencies=[Depends(require_strm_enabled)],
)
async def get_cleanup_plan(plan_id: str, request: Request) -> StrmCleanupPlanResponse:
    service = StrmCleanupPlanService(request.app.state.database.session_factory)
    try:
        plan = await service.get_plan(plan_id)
    except StrmCleanupPlanError as error:
        status = 404 if error.code == "plan_not_found" else 409
        raise HTTPException(status_code=status, detail=error.code) from None
    return StrmCleanupPlanResponse.model_validate(plan.to_public_dict())


@router.post(
    "/libraries/{library_id}/strm-verify",
    response_model=StrmVerifyResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:read")),
    ],
)
async def verify_manifest(
    library_id: str,
    payload: StrmVerifyRequest,
    request: Request,
    context: AuthDependency,
) -> StrmVerifyResponse:
    try:
        result = await StrmVerificationService(
            request.app.state.database.session_factory
        ).verify(
            library_id=library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=getattr(request.app.state, "strm_output_root", "./data/strm"),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmVerificationError as error:
        raise HTTPException(status_code=409, detail=error.code) from None
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        await settings_service.log_event(
            "strm.verify.completed",
            fields={"status": result.status},
            counts={"count": result.checked_count},
            actor_type="agent" if context.via_bearer else "web",
            actor_id=context.identity,
            resource_type="library",
            resource_id=library_id,
        )
    return StrmVerifyResponse.model_validate(result.to_public_dict())


@router.post(
    "/libraries/{library_id}/strm-cleanup",
    dependencies=[
        Depends(require_strm_cleanup_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def cleanup_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    request: Request,
) -> None:
    """Keep the legacy path safe while clients migrate to plan/apply."""

    # Validate the supplied snapshot and create a reviewable plan so this
    # compatibility route never performs a retirement without confirmation.
    service = StrmCleanupPlanService(request.app.state.database.session_factory)
    try:
        plan = await service.create_plan(
            library_id=library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=getattr(request.app.state, "strm_output_root", "./data/strm"),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmCleanupPlanError as error:
        raise HTTPException(status_code=409, detail=error.code) from None
    raise HTTPException(
        status_code=409,
        detail={
            "code": "cleanup_plan_required",
            "message": "STRM 失效清理必须先预览并确认清理计划",
            "plan_id": plan.plan_id,
        },
    )


@router.api_route(
    "/strm/play/{manifest_id}",
    methods=["GET", "HEAD"],
    dependencies=[Depends(require_strm_playback_enabled)],
)
async def play_manifest(
    manifest_id: str,
    request: Request,
    context: AuthDependency,
) -> Response:
    try:
        playback_request = make_playback_request(
            manifest_id,
            request.method,
            request.headers.get("range"),
        )
    except PlaybackContractError as error:
        raise HTTPException(status_code=416, detail="invalid_playback_request") from error
    gateway = getattr(request.app.state, "strm_playback_gateway", None)
    if gateway is None or not callable(getattr(gateway, "resolve", None)):
        raise HTTPException(status_code=503, detail="strm_playback_unavailable")
    allowed_library_ids = (
        context.library_ids
        if context.via_bearer and context.library_ids
        else None
    )
    outcome = await gateway.resolve(
        playback_request,
        gate=PlaybackGate(
            enabled=bool(getattr(request.app.state, "strm_playback_enabled", False)),
            contract_verified=bool(
                getattr(request.app.state, "strm_playback_contract_verified", False)
            ),
        ),
        allowed_library_ids=allowed_library_ids,
    )
    if outcome.status is not PlaybackStatus.READY or outcome.url is None:
        raise _playback_error(outcome.status)
    return await _proxy_playback(request, playback_request, outcome.url, outcome.request_headers)


async def _proxy_playback(
    request: Request,
    playback_request,
    upstream_url: str,
    upstream_headers: tuple[tuple[str, str], ...],
) -> Response:
    headers = {name: value for name, value in upstream_headers}
    if playback_request.method.value == "GET" and playback_request.byte_range is not None:
        headers["Range"] = _range_header(playback_request.byte_range)
    client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
    try:
        upstream = await client.send(
            client.build_request(playback_request.method.value, upstream_url, headers=headers),
            stream=True,
        )
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(status_code=504, detail="playback_timeout") from None
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="playback_remote_failed") from None
    if upstream.status_code not in {200, 206, 404, 410, 416}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="playback_remote_failed")
    if upstream.status_code in {404, 410}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=404, detail="playback_file_not_found")
    response_headers = _allowed_upstream_headers(upstream.headers)
    if playback_request.method.value == "HEAD":
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=upstream.status_code, headers=response_headers)

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=response_headers
    )


def _playback_error(status: PlaybackStatus) -> HTTPException:
    if status is PlaybackStatus.NOT_FOUND:
        return HTTPException(status_code=404, detail="playback_file_not_found")
    if status is PlaybackStatus.UNCERTAIN:
        return HTTPException(status_code=504, detail="playback_timeout")
    if status is PlaybackStatus.DISABLED:
        return HTTPException(status_code=503, detail="strm_playback_disabled")
    if status is PlaybackStatus.UNVERIFIED:
        return HTTPException(status_code=503, detail="strm_playback_unverified")
    return HTTPException(status_code=502, detail="playback_remote_failed")


def _range_header(byte_range) -> str:
    start = "" if byte_range.start is None else str(byte_range.start)
    end = "" if byte_range.end is None else str(byte_range.end)
    return f"bytes={start}-{end}"


def _allowed_upstream_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "accept-ranges",
        "cache-control",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
    }
    return {key: value for key, value in headers.items() if key.casefold() in allowed}


def _require_playback_network(request: Request) -> None:
    client = request.client
    if client is None:
        raise HTTPException(status_code=403, detail="playback_network_forbidden")
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        raise HTTPException(status_code=403, detail="playback_network_forbidden") from None
    networks: Collection[object] = getattr(
        request.app.state, "strm_playback_allowed_networks", ()
    )
    if not any(address in network for network in networks):
        raise HTTPException(status_code=403, detail="playback_network_forbidden")


__all__ = ["router"]
