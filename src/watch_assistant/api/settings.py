"""Authenticated settings overview and redacted log routes."""

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from watch_assistant.models import OrganizationOperation, OrganizationOperationStatus
from watch_assistant.schemas import (
    CapabilityAvailabilityResponse,
    CapabilityState,
    CapabilityStatusResponse,
    ContentPolicyPatch,
    ContentPolicyResponse,
    InspectionSettingsPatch,
    InspectionSettingsResponse,
    LogCategory,
    LoggingLevel,
    LoggingSettingsPatch,
    LoggingSettingsResponse,
    LogItem,
    LogsResponse,
    OrganizationAutomationResultResponse,
    OrganizationBlockedDetailResponse,
    OrganizationResultItemResponse,
    OrganizationScheduleActionResponse,
    OrganizationSettingsPatch,
    OrganizationSettingsResponse,
    SettingsOverviewResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.organization_capability import (
    organization_execution_supported,
)
from watch_assistant.services.settings import (
    ContentPolicyValidationError,
    OrganizationSettingsValidationError,
    SettingsConflict,
    SettingsService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])

ORGANIZATION_RESULT_STATUSES = [
    "unknown",
    "success",
    "skipped",
    "deleted",
    "replace",
    "failed",
]


def _organization_result_response(result) -> OrganizationAutomationResultResponse:
    if result is None:
        return OrganizationAutomationResultResponse(
            status="unknown",
            available_statuses=ORGANIZATION_RESULT_STATUSES,
            source_count=0,
            scanned_count=0,
            plan_count=0,
            queued_count=0,
            blocked_count=0,
            blocked_details=[],
            run_id=None,
        )
    return OrganizationAutomationResultResponse(
        status=(
            "failed"
            if result.blocked_count
            else "skipped"
            if result.plan_count and result.queued_count == 0
            else "success"
        ),
        available_statuses=ORGANIZATION_RESULT_STATUSES,
        source_count=result.source_count,
        scanned_count=result.scanned_count,
        plan_count=result.plan_count,
        queued_count=result.queued_count,
        blocked_count=result.blocked_count,
        blocked_details=[
            OrganizationBlockedDetailResponse.model_validate(detail.to_public_dict())
            for detail in result.blocked_details
        ],
        finished_at=result.finished_at,
        run_id=result.run_id,
        cleaned_small_files=result.cleaned_small_files,
        cleaned_empty_dirs=result.cleaned_empty_dirs,
    )


def get_settings_service(request: Request) -> SettingsService:
    service = getattr(request.app.state, "settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="settings_unavailable")
    return service


SettingsDependency = Annotated[SettingsService, Depends(get_settings_service)]


async def require_content_policy_write_access(
    auth: Annotated[AuthContext, Depends(require_api_auth)],
    settings: SettingsDependency,
) -> None:
    try:
        settings.check_write_rate_limit(auth.identity)
    except SettingsConflict as exc:
        raise HTTPException(status_code=429, detail="rate_limited") from exc


@router.get("/settings/overview", response_model=SettingsOverviewResponse)
async def settings_overview(request: Request) -> SettingsOverviewResponse:
    database = getattr(request.app.state, "database", None)
    empty_cleanup_setting = False
    settings_service = getattr(request.app.state, "settings_service", None)
    if settings_service is not None:
        try:
            empty_cleanup_setting = (
                await settings_service.get_organization()
            ).cleanup_empty_directories
        except Exception:  # noqa: BLE001 - capability diagnostics stay conservative
            empty_cleanup_setting = False
    execution_supported = organization_execution_supported(request.app)
    capabilities = _collect_capabilities(
        request.app.state, execution_supported, empty_cleanup_setting
    )
    strm_cleanup_enabled = capabilities["strm_cleanup"]
    empty_cleanup_enabled = capabilities["organization_empty_directory_cleanup"]
    automation = getattr(request.app.state, "organization_automation_service", None)
    last_result = getattr(automation, "last_result", None)
    plan_success_at = (
        last_result.finished_at
        if last_result is not None
        and last_result.finished_at is not None
        and last_result.blocked_count == 0
        else None
    )
    execution_success_at = await _latest_organization_success_at(database)
    worker_running = execution_supported
    write_contract_verified = bool(
        getattr(request.app.state, "organization_write_contract_verified", False)
    )
    return SettingsOverviewResponse(
        release=request.app.state.release,
        uptime_seconds=max(0, int(time.monotonic() - request.app.state.started_at)),
        database_size_bytes=_database_size(database),
        capabilities=capabilities,
        capability_statuses=_collect_capability_statuses(
            app_state=request.app.state,
            capabilities=capabilities,
            write_contract_verified=write_contract_verified,
            empty_cleanup_setting=empty_cleanup_setting,
            worker_running=worker_running,
            plan_success_at=plan_success_at,
            execution_success_at=execution_success_at,
        ),
        capability_details={
            "strm_cleanup": CapabilityAvailabilityResponse(
                enabled=strm_cleanup_enabled,
                reason_code=None if strm_cleanup_enabled else "strm_cleanup_disabled",
                reason_zh=(
                    "可用"
                    if strm_cleanup_enabled
                    else "STRM 失效清理当前未启用。"
                ),
                settings_section="overview",
            ),
            "organization_empty_directory_cleanup": CapabilityAvailabilityResponse(
                enabled=empty_cleanup_enabled,
                reason_code=(
                    None
                    if empty_cleanup_enabled
                    else "empty_directory_cleanup_disabled"
                ),
                reason_zh=(
                    "可用"
                    if empty_cleanup_enabled
                    else "空目录回收当前未就绪。"
                ),
                settings_section="organization",
            ),
        },
    )


def _collect_capabilities(
    app_state, execution_supported: bool, empty_cleanup_setting: bool
) -> dict[str, bool]:
    """Collect the 14 capability booleans from application state.

    This is the single place that reads app state for capability readiness; the
    status presentation in ``_collect_capability_statuses`` derives from these
    booleans plus a few deliberately looser "configured" preconditions.
    """
    write_contract_verified = bool(
        getattr(app_state, "organization_write_contract_verified", False)
    )
    return {
        "inspection": bool(getattr(app_state, "inspection_supported", False)),
        "magnet": bool(getattr(app_state, "push_capabilities", {}).get("magnet", False)),
        "share": bool(getattr(app_state, "push_capabilities", {}).get("share", False)),
        "organization_plan": bool(
            getattr(app_state, "organization_plan_enabled", False)
        ),
        "organization_execution": bool(execution_supported),
        "organization_write": bool(
            execution_supported
            and getattr(app_state, "organization_write_enabled", False)
            and write_contract_verified
        ),
        "permanent_delete": bool(
            execution_supported
            and getattr(app_state, "organization_write_enabled", False)
            and write_contract_verified
            and getattr(app_state, "permanent_delete_enabled", False)
            and getattr(app_state, "permanent_delete_contract_verified", False)
        ),
        "strm_full": bool(getattr(app_state, "strm_full_enabled", False)),
        "strm_incremental": bool(
            getattr(app_state, "strm_incremental_enabled", False)
        ),
        "strm_cleanup": bool(getattr(app_state, "strm_cleanup_enabled", False)),
        "strm_playback": bool(
            getattr(app_state, "strm_playback_enabled", False)
            and getattr(app_state, "strm_playback_supported", False)
            and getattr(app_state, "strm_playback_contract_verified", False)
        ),
        "organization_empty_directory_cleanup": bool(
            empty_cleanup_setting
            and callable(
                getattr(app_state, "empty_directory_cleanup_executor", None)
            )
        ),
    }


def _collect_capability_statuses(
    *,
    app_state,
    capabilities: dict[str, bool],
    write_contract_verified: bool,
    empty_cleanup_setting: bool,
    worker_running: bool,
    plan_success_at,
    execution_success_at,
) -> dict[str, CapabilityStatusResponse]:
    """Derive capability statuses from the collected booleans.

    ``configured`` intentionally uses looser preconditions than the capability
    boolean for write/delete/playback: a prerequisite present but not yet fully
    ready renders as "已配置但关闭" instead of "未配置".
    """
    return {
        "inspection": _capability_status(
            configured=capabilities["inspection"],
            runtime_healthy=capabilities["inspection"],
        ),
        "magnet": _capability_status(
            configured=capabilities["magnet"],
            runtime_healthy=capabilities["magnet"],
        ),
        "share": _capability_status(configured=capabilities["share"]),
        "organization_plan": _capability_status(
            configured=capabilities["organization_plan"],
            last_success_at=plan_success_at,
        ),
        "organization_execution": _capability_status(
            configured=capabilities["organization_execution"],
            runtime_healthy=worker_running,
            last_success_at=execution_success_at,
        ),
        "organization_write": _capability_status(
            configured=(
                bool(getattr(app_state, "organization_write_enabled", False))
                or write_contract_verified
            ),
            contract_verified=write_contract_verified,
            runtime_healthy=worker_running and capabilities["organization_write"],
            last_success_at=execution_success_at,
        ),
        "permanent_delete": _capability_status(
            configured=bool(getattr(app_state, "permanent_delete_enabled", False)),
            contract_verified=bool(
                getattr(app_state, "permanent_delete_contract_verified", False)
            ),
        ),
        "strm_full": _capability_status(configured=capabilities["strm_full"]),
        "strm_incremental": _capability_status(
            configured=capabilities["strm_incremental"]
        ),
        "strm_cleanup": _capability_status(configured=capabilities["strm_cleanup"]),
        "strm_playback": _capability_status(
            configured=bool(getattr(app_state, "strm_playback_enabled", False)),
            contract_verified=bool(
                getattr(app_state, "strm_playback_contract_verified", False)
            ),
            runtime_healthy=capabilities["strm_playback"],
        ),
        "organization_empty_directory_cleanup": _capability_status(
            configured=empty_cleanup_setting,
            runtime_healthy=capabilities["organization_empty_directory_cleanup"],
        ),
    }


_CAPABILITY_STATE_ZH = {
    CapabilityState.UNCONFIGURED: "未配置",
    CapabilityState.CONFIGURED: "已配置",
    CapabilityState.CONTRACT_VERIFIED: "契约已验证",
    CapabilityState.RUNTIME_HEALTHY: "运行健康",
    CapabilityState.RECENT_SUCCESS: "最近成功",
}


def _capability_status(
    *,
    configured: bool,
    contract_verified: bool = False,
    runtime_healthy: bool = False,
    last_success_at=None,
) -> CapabilityStatusResponse:
    state = (
        CapabilityState.RECENT_SUCCESS
        if last_success_at is not None
        else CapabilityState.RUNTIME_HEALTHY
        if runtime_healthy
        else CapabilityState.CONTRACT_VERIFIED
        if contract_verified
        else CapabilityState.CONFIGURED
        if configured
        else CapabilityState.UNCONFIGURED
    )
    return CapabilityStatusResponse(
        state=state,
        state_zh=_CAPABILITY_STATE_ZH[state],
        last_success_at=last_success_at,
    )


async def _latest_organization_success_at(database):
    if database is None:
        return None
    async with database.session_factory() as session:
        return await session.scalar(
            select(OrganizationOperation.finished_at)
            .where(
                OrganizationOperation.status == OrganizationOperationStatus.ORGANIZED,
                OrganizationOperation.finished_at.is_not(None),
            )
            .order_by(OrganizationOperation.finished_at.desc())
            .limit(1)
        )


@router.get("/settings/logging", response_model=LoggingSettingsResponse)
async def get_logging(settings: SettingsDependency) -> LoggingSettingsResponse:
    return await settings.get_logging()


@router.patch("/settings/logging", response_model=LoggingSettingsResponse)
async def patch_logging(
    patch: LoggingSettingsPatch,
    settings: SettingsDependency,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> LoggingSettingsResponse:
    try:
        return await settings.update_logging(
            patch,
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc


@router.get("/settings/content-policy", response_model=ContentPolicyResponse)
async def get_content_policy(settings: SettingsDependency) -> ContentPolicyResponse:
    return await settings.get_content_policy()


@router.patch("/settings/content-policy", response_model=ContentPolicyResponse)
async def patch_content_policy(
    patch: ContentPolicyPatch,
    settings: SettingsDependency,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
    _: Annotated[None, Depends(require_content_policy_write_access)],
) -> ContentPolicyResponse:
    try:
        return await settings.update_content_policy(
            patch,
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc
    except ContentPolicyValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid_content_policy") from exc


@router.get("/settings/inspection", response_model=InspectionSettingsResponse)
async def get_inspection(settings: SettingsDependency) -> InspectionSettingsResponse:
    return await settings.get_inspection()


@router.patch("/settings/inspection", response_model=InspectionSettingsResponse)
async def patch_inspection(
    patch: InspectionSettingsPatch,
    settings: SettingsDependency,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> InspectionSettingsResponse:
    try:
        return await settings.update_inspection(
            patch,
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc


@router.get("/settings/organization", response_model=OrganizationSettingsResponse)
async def get_organization(settings: SettingsDependency) -> OrganizationSettingsResponse:
    return await settings.get_organization()


@router.patch("/settings/organization", response_model=OrganizationSettingsResponse)
async def patch_organization(
    patch: OrganizationSettingsPatch,
    settings: SettingsDependency,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> OrganizationSettingsResponse:
    configured_root_id = getattr(request.app.state, "organization_target_root_id", None)
    browsed_directory_ids = getattr(request.app.state, "p115_browsed_directory_ids", set())
    requested_directory_ids: set[str] = set()
    patch_values = patch.model_dump(exclude_unset=True)
    for key in ("source_directory_ids", "target_directory_id", "push_directory_id"):
        value = patch_values.get(key)
        if isinstance(value, list):
            requested_directory_ids.update(item for item in value if isinstance(item, str))
        elif isinstance(value, str):
            requested_directory_ids.add(value)
    if requested_directory_ids:
        if isinstance(configured_root_id, str) and configured_root_id:
            # 已配置 root:所有目录必须落在 root/已浏览集合内,防止越权目录。
            allowed = {configured_root_id, *browsed_directory_ids}
            if not requested_directory_ids.issubset(allowed):
                raise HTTPException(status_code=403, detail="p115_directory_out_of_scope")
        else:
            # 目标根未配置:target_directory_id 是根配置的来源,允许首次设置;
            # 但 source/push 没有 root 可核对,必须 fail-closed 拒绝,不得
            # 静默放行(唯一兜底只剩运行期写契约)。
            requested_target = patch_values.get("target_directory_id")
            non_target = requested_directory_ids - (
                {requested_target} if isinstance(requested_target, str) else set()
            )
            if non_target:
                raise HTTPException(status_code=403, detail="p115_directory_out_of_scope")
        patch_values = patch.model_dump(exclude_unset=True)
        sources = patch_values.get("source_directory_ids")
        target = patch_values.get("target_directory_id")
        if (
            isinstance(target, str)
            and isinstance(sources, list)
            and target in sources
        ):
            # 与设置服务校验保持同一错误码(source_target_same)。
            raise HTTPException(
                status_code=422,
                detail="source_target_same",
            )
    try:
        response = await settings.update_organization(
            patch,
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
        controller = getattr(request.app.state, "organization_scheduler", None)
        notify = getattr(controller, "notify_settings_changed", None)
        if callable(notify):
            notify()
        # 同步运行时 state(与启动时 apply_p115_runtime 的推导一致):
        # target root 由 worker 每次执行通过 provider 读取,避免沿用启动
        # 快照导致目标目录变更后整理错位/反复失败;browsed 目录集保持与
        # 最新设置一致,后续 PATCH 的目录范围校验才不会误拒。
        configured_target = response.target_directory_id
        request.app.state.organization_target_root_id = (
            str(configured_target)
            if isinstance(configured_target, str) and configured_target.isdigit()
            else None
        )
        configured_directory_ids = {
            directory_id
            for directory_id in (
                *response.source_directory_ids,
                response.target_directory_id,
                response.push_directory_id,
            )
            if isinstance(directory_id, str) and directory_id.isdigit()
        }
        if request.app.state.organization_target_root_id:
            configured_directory_ids.add(
                request.app.state.organization_target_root_id
            )
        request.app.state.p115_browsed_directory_ids = configured_directory_ids
        return response
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc
    except OrganizationSettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post(
    "/settings/organization/run-now",
    response_model=OrganizationScheduleActionResponse,
)
async def run_organization_now(
    request: Request,
    settings: SettingsDependency,
) -> OrganizationScheduleActionResponse:
    controller = getattr(request.app.state, "organization_scheduler", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="organization_schedule_unavailable")
    run_id = await controller.request_run_now()
    if not isinstance(run_id, str) or not run_id:
        run_id = None
    current = await settings.get_organization()
    return OrganizationScheduleActionResponse(
        action="run_now",
        queued=True,
        schedule_enabled=current.schedule_enabled,
        message_zh="整理已排队，当前任务完成后将立即执行。",
        run_id=run_id,
    )


@router.get(
    "/settings/organization/result",
    response_model=OrganizationAutomationResultResponse,
)
async def get_organization_result(
    request: Request,
) -> OrganizationAutomationResultResponse:
    automation = getattr(request.app.state, "organization_automation_service", None)
    response = _organization_result_response(
        getattr(automation, "last_result", None) if automation is not None else None
    )
    get_items = getattr(automation, "result_items", None)
    if callable(get_items):
        response.items = [
            OrganizationResultItemResponse(
                title=item.title,
                tmdb_id=item.tmdb_id,
                target=item.target,
                status=item.status,
                error_code=item.error_code,
            )
            for item in await get_items()
        ]
    return response


@router.post(
    "/settings/organization/stop",
    response_model=OrganizationScheduleActionResponse,
)
async def stop_organization(
    request: Request,
    settings: SettingsDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> OrganizationScheduleActionResponse:
    current = await settings.get_organization()
    await settings.update_organization(
        OrganizationSettingsPatch(revision=current.revision, schedule_enabled=False),
        actor_type="agent" if auth.via_bearer else "web",
        actor_id=auth.identity,
        request_id=request.headers.get("X-Request-ID"),
    )
    controller = getattr(request.app.state, "organization_scheduler", None)
    if controller is not None:
        controller.stop_pending()
    return OrganizationScheduleActionResponse(
        action="stop",
        queued=False,
        schedule_enabled=False,
        message_zh="已关闭定时整理，并停止尚未开始的整理请求；正在执行的远端操作继续按安全流程收尾。",
        run_id=None,
    )


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    settings: SettingsDependency,
    cursor: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    category: LogCategory | None = None,
    level: LoggingLevel | None = None,
    event_code: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    request_id: str | None = Query(default=None, max_length=128),
    correlation_id: str | None = Query(default=None, max_length=128),
    task_id: str | None = Query(default=None, max_length=128),
    actor_type: str | None = Query(default=None, max_length=32),
    actor_id: str | None = Query(default=None, max_length=128),
    resource_type: str | None = Query(default=None, max_length=64),
    resource_id: str | None = Query(default=None, max_length=128),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> LogsResponse:
    items, next_cursor = await settings.log_store.list(
        cursor=cursor,
        limit=limit,
        category=category,
        level=level,
        event_code=event_code,
        status=status,
        request_id=request_id,
        correlation_id=correlation_id,
        task_id=task_id,
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        start_time=start_time,
        end_time=end_time,
    )
    return LogsResponse(
        items=[LogItem.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.get("/logs/export", response_class=PlainTextResponse)
async def export_logs(
    settings: SettingsDependency,
    format: str = Query(default="jsonl", pattern="^(jsonl|csv)$"),
    limit: int = Query(default=50000, ge=1, le=200000),
    category: LogCategory | None = None,
    level: LoggingLevel | None = None,
    event_code: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    request_id: str | None = Query(default=None, max_length=128),
    correlation_id: str | None = Query(default=None, max_length=128),
    task_id: str | None = Query(default=None, max_length=128),
    actor_type: str | None = Query(default=None, max_length=32),
    actor_id: str | None = Query(default=None, max_length=128),
    resource_type: str | None = Query(default=None, max_length=64),
    resource_id: str | None = Query(default=None, max_length=128),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> PlainTextResponse:
    """导出日志,带上限保护:全量累积进内存会构成已认证 DoS。"""
    records: list[dict[str, object]] = []
    cursor: int | None = None
    while len(records) < limit:
        page_size = min(100, limit - len(records))
        page, cursor = await settings.log_store.list(
            cursor=cursor,
            limit=page_size,
            category=category,
            level=level,
            event_code=event_code,
            status=status,
            request_id=request_id,
            correlation_id=correlation_id,
            task_id=task_id,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            start_time=start_time,
            end_time=end_time,
        )
        records.extend(page)
        if cursor is None:
            break
    if format == "csv":
        output = io.StringIO()
        fieldnames = [
            "id", "timestamp", "level", "category", "event_code", "event_version",
            "title_zh", "message_zh", "suggestion_zh", "status", "request_id",
            "correlation_id", "actor_type", "actor_id", "resource_type", "resource_id",
            "task_id", "duration_ms", "counts", "error_code", "context",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["counts"] = json.dumps(row.get("counts", {}), ensure_ascii=False)
            row["context"] = json.dumps(row.get("context", {}), ensure_ascii=False)
            writer.writerow(row)
        return PlainTextResponse(output.getvalue(), media_type="text/csv; charset=utf-8")
    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    return PlainTextResponse(body, media_type="application/x-ndjson; charset=utf-8")


def _database_size(database: object) -> int:
    if database is None:
        return 0
    try:
        database_path = database.engine.url.database
        if not database_path or database_path == ":memory:":
            return 0
        return max(0, Path(database_path).stat().st_size)
    except (AttributeError, OSError, TypeError):
        return 0
