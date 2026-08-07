"""Authenticated magnet inspection batch routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import InspectionBatchResponse, InspectionStartRequest
from watch_assistant.security import require_api_auth
from watch_assistant.services.inspection import (
    InspectionBatchNotFound,
    InspectionResourceInvalid,
    InspectionService,
)
from watch_assistant.services.workflows import WorkflowConflict, WorkflowNotFound

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_inspection_service(request: Request) -> InspectionService:
    if getattr(request.app.state, "inspection_supported", False) is not True:
        raise HTTPException(status_code=503, detail="inspection_unsupported")
    service = getattr(request.app.state, "inspection_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="inspection_unsupported")
    return service


InspectionServiceDependency = Annotated[InspectionService, Depends(get_inspection_service)]


@router.post(
    "/resources/inspect",
    response_model=InspectionBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_inspection(
    payload: InspectionStartRequest,
    service: InspectionServiceDependency,
) -> InspectionBatchResponse:
    try:
        return await service.create(
            payload.resource_ids,
            force=payload.force,
            workflow_id=payload.workflow_id,
        )
    except InspectionResourceInvalid as exc:
        raise HTTPException(status_code=422, detail="resource_not_inspectable") from exc
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code not in {
            "workflow_prerequisite_not_met",
            "workflow_stage_regression",
            "workflow_stage_terminal",
            "workflow_evidence_required",
        }:
            code = "workflow_conflict"
        raise HTTPException(status_code=409, detail=code) from None


@router.get(
    "/resources/inspect/{batch_id}", response_model=InspectionBatchResponse
)
async def get_inspection(
    batch_id: str, service: InspectionServiceDependency
) -> InspectionBatchResponse:
    try:
        return await service.get(batch_id)
    except InspectionBatchNotFound as exc:
        raise HTTPException(status_code=404, detail="inspection_not_found") from exc
