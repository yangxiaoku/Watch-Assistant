"""Authenticated local backup inventory routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import (
    BackupConfigurationExportResponse,
    BackupConfigurationImportRequest,
    BackupConfigurationImportResponse,
    BackupListResponse,
    BackupResponse,
    BackupRestorePreviewResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.backups import BackupService, BackupServiceError

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_backup_service(request: Request) -> BackupService:
    service = getattr(request.app.state, "backup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="backups_unavailable")
    return service


ServiceDependency = Annotated[BackupService, Depends(get_backup_service)]


@router.get(
    "/backups/configuration",
    response_model=BackupConfigurationExportResponse,
)
async def export_configuration(
    service: ServiceDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
    request: Request,
) -> BackupConfigurationExportResponse:
    if (
        auth.via_bearer
        and auth.identity != "internal"
        and not auth.has_scope("settings:read")
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "missing_scope", "missing_scopes": ["settings:read"]},
        )
    try:
        return await service.export_configuration(
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
    except BackupServiceError as exc:
        status_code = 503 if exc.code == "backup_configuration_unavailable" else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc


@router.post(
    "/backups/configuration/import",
    response_model=BackupConfigurationImportResponse,
)
async def import_configuration(
    payload: BackupConfigurationImportRequest,
    service: ServiceDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
    request: Request,
) -> BackupConfigurationImportResponse:
    if (
        auth.via_bearer
        and auth.identity != "internal"
        and not auth.has_scope("settings:write")
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "missing_scope", "missing_scopes": ["settings:write"]},
        )
    try:
        response = await service.import_configuration(
            payload,
            actor_type="agent" if auth.via_bearer else "web",
            actor_id=auth.identity,
            request_id=request.headers.get("X-Request-ID"),
        )
        controller = getattr(request.app.state, "organization_scheduler", None)
        notify = getattr(controller, "notify_settings_changed", None)
        if callable(notify):
            notify()
        return response
    except BackupServiceError as exc:
        status_by_code = {
            "backup_configuration_conflict": 409,
            "backup_configuration_unavailable": 503,
        }
        raise HTTPException(
            status_code=status_by_code.get(exc.code, 422), detail=exc.code
        ) from exc


@router.post(
    "/backups", response_model=BackupResponse, status_code=status.HTTP_201_CREATED
)
async def create_backup(service: ServiceDependency) -> BackupResponse:
    try:
        return await service.create()
    except BackupServiceError as exc:
        code = exc.code
        http_status = 409 if code == "backup_requires_file_database" else 503
        raise HTTPException(status_code=http_status, detail=code) from exc


@router.get("/backups", response_model=BackupListResponse)
async def list_backups(service: ServiceDependency) -> BackupListResponse:
    return await service.list()


@router.get("/backups/{backup_id}/restore-preview", response_model=BackupRestorePreviewResponse)
async def preview_restore(
    backup_id: str, service: ServiceDependency
) -> BackupRestorePreviewResponse:
    try:
        return await service.preview_restore(backup_id)
    except BackupServiceError as exc:
        status_code = 404 if exc.code == "backup_not_found" else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc
