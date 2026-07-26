"""Authenticated settings overview and redacted log routes."""

import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.schemas import (
    InspectionSettingsPatch,
    InspectionSettingsResponse,
    LogCategory,
    LoggingSettingsPatch,
    LoggingSettingsResponse,
    LogItem,
    LogsResponse,
    SettingsOverviewResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.settings import SettingsConflict, SettingsService

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_settings_service(request: Request) -> SettingsService:
    service = getattr(request.app.state, "settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="settings_unavailable")
    return service


SettingsDependency = Annotated[SettingsService, Depends(get_settings_service)]


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
        },
    )


@router.get("/settings/logging", response_model=LoggingSettingsResponse)
async def get_logging(settings: SettingsDependency) -> LoggingSettingsResponse:
    return await settings.get_logging()


@router.patch("/settings/logging", response_model=LoggingSettingsResponse)
async def patch_logging(
    patch: LoggingSettingsPatch,
    settings: SettingsDependency,
) -> LoggingSettingsResponse:
    try:
        return await settings.update_logging(patch)
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc


@router.get("/settings/inspection", response_model=InspectionSettingsResponse)
async def get_inspection(settings: SettingsDependency) -> InspectionSettingsResponse:
    return await settings.get_inspection()


@router.patch("/settings/inspection", response_model=InspectionSettingsResponse)
async def patch_inspection(
    patch: InspectionSettingsPatch,
    settings: SettingsDependency,
) -> InspectionSettingsResponse:
    try:
        return await settings.update_inspection(patch)
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail="settings_conflict") from exc


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    settings: SettingsDependency,
    cursor: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    category: LogCategory | None = None,
) -> LogsResponse:
    items, next_cursor = await settings.log_store.list(
        cursor=cursor,
        limit=limit,
        category=category,
    )
    return LogsResponse(
        items=[LogItem.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


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
