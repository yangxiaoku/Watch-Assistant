"""Authenticated preview-first manual resource import routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import (
    ManualImportPreviewResponse,
    ManualImportRequest,
    ManualImportResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.manual_import import (
    ManualImportError,
    ManualImportService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_manual_import_service(request: Request) -> ManualImportService:
    service = getattr(request.app.state, "manual_import_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="manual_import_unavailable")
    return service


ServiceDependency = Annotated[ManualImportService, Depends(get_manual_import_service)]


@router.post("/imports/preview", response_model=ManualImportPreviewResponse)
async def preview_import(
    payload: ManualImportRequest, service: ServiceDependency
) -> ManualImportPreviewResponse:
    try:
        return await service.preview(payload)
    except ManualImportError as exc:
        status_code = 409 if exc.code == "media_mismatch" else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc


@router.post(
    "/imports", response_model=ManualImportResponse, status_code=status.HTTP_201_CREATED
)
async def confirm_import(
    payload: ManualImportRequest, service: ServiceDependency
) -> ManualImportResponse:
    try:
        return await service.confirm(payload)
    except ManualImportError as exc:
        status_code = 409 if exc.code in {"confirmation_required", "media_mismatch"} else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc
