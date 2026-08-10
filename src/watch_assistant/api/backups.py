"""Authenticated local backup inventory routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import (
    BackupListResponse,
    BackupResponse,
    BackupRestorePreviewResponse,
)
from watch_assistant.security import AuthContext, require_api_auth, require_scope
from watch_assistant.services.backups import BackupService, BackupServiceError

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_backup_service(request: Request) -> BackupService:
    service = getattr(request.app.state, "backup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="backups_unavailable")
    return service


ServiceDependency = Annotated[BackupService, Depends(get_backup_service)]
BackupReadDependency = Annotated[AuthContext, Depends(require_scope("backup:read"))]
BackupWriteDependency = Annotated[AuthContext, Depends(require_scope("backup:write"))]


@router.post(
    "/backups", response_model=BackupResponse, status_code=status.HTTP_201_CREATED
)
async def create_backup(
    service: ServiceDependency, _context: BackupWriteDependency
) -> BackupResponse:
    try:
        return await service.create()
    except BackupServiceError as exc:
        code = exc.code
        http_status = 409 if code == "backup_requires_file_database" else 503
        raise HTTPException(status_code=http_status, detail=code) from exc


@router.get("/backups", response_model=BackupListResponse)
async def list_backups(
    service: ServiceDependency, _context: BackupReadDependency
) -> BackupListResponse:
    return await service.list()


@router.get("/backups/{backup_id}/restore-preview", response_model=BackupRestorePreviewResponse)
async def preview_restore(
    backup_id: str,
    service: ServiceDependency,
    _context: BackupReadDependency,
) -> BackupRestorePreviewResponse:
    try:
        return await service.preview_restore(backup_id)
    except BackupServiceError as exc:
        status_code = 404 if exc.code == "backup_not_found" else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc
