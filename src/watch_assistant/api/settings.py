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

from watch_assistant.schemas import (
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
    SettingsOverviewResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.settings import (
    ContentPolicyValidationError,
    SettingsConflict,
    SettingsService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


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
    return SettingsOverviewResponse(
        release=request.app.state.release,
        uptime_seconds=max(0, int(time.monotonic() - request.app.state.started_at)),
        database_size_bytes=_database_size(database),
        capabilities={
            "inspection": bool(
                getattr(request.app.state, "inspection_supported", False)
            ),
            "magnet": bool(
                getattr(request.app.state, "push_capabilities", {}).get("magnet", False)
            ),
            "share": bool(
                getattr(request.app.state, "push_capabilities", {}).get("share", False)
            ),
            "organization_plan": bool(
                getattr(request.app.state, "organization_plan_enabled", False)
            ),
            "organization_execution": bool(
                getattr(request.app.state, "organization_execution_supported", False)
                or getattr(request.app.state, "organization_worker", None) is not None
            ),
            "organization_write": bool(
                getattr(request.app.state, "organization_worker", None) is not None
                and getattr(request.app.state, "organization_write_enabled", False)
                and getattr(
                    request.app.state, "organization_write_contract_verified", False
                )
            ),
            "permanent_delete": bool(
                getattr(request.app.state, "organization_worker", None) is not None
                and getattr(request.app.state, "organization_write_enabled", False)
                and getattr(
                    request.app.state, "organization_write_contract_verified", False
                )
                and getattr(request.app.state, "permanent_delete_enabled", False)
                and getattr(
                    request.app.state, "permanent_delete_contract_verified", False
                )
            ),
            "strm_full": bool(getattr(request.app.state, "strm_full_enabled", False)),
            "strm_incremental": bool(
                getattr(request.app.state, "strm_incremental_enabled", False)
            ),
            "strm_cleanup": bool(
                getattr(request.app.state, "strm_cleanup_enabled", False)
            ),
            "strm_playback": bool(
                getattr(request.app.state, "strm_playback_enabled", False)
                and getattr(
                    request.app.state, "strm_playback_contract_verified", False
                )
            ),
        },
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
    records: list[dict[str, object]] = []
    cursor: int | None = None
    while True:
        page, cursor = await settings.log_store.list(
            cursor=cursor,
            limit=100,
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
