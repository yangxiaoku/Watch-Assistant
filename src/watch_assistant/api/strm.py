"""Authenticated STRM manifest and generation routes."""

import math
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.schemas import (
    StrmGenerationRequest,
    StrmGenerationResponse,
    StrmManifestItemResponse,
    StrmManifestListResponse,
)
from watch_assistant.security import require_api_auth, require_scope
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    StrmManifestService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


async def require_strm_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_full_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_full_disabled", "message": "STRM 功能未启用"},
        )


def _service(request: Request) -> StrmManifestService:
    service = getattr(request.app.state, "strm_manifest_service", None)
    if not isinstance(service, StrmManifestService):
        raise HTTPException(status_code=503, detail="strm_unavailable")
    return service


ServiceDependency = Annotated[StrmManifestService, Depends(_service)]


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
    try:
        summary = await service.generate(
            library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=Path(
                getattr(request.app.state, "strm_output_root", "./data/strm")
            ),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmGenerationResponse(
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
    )


__all__ = ["router"]
