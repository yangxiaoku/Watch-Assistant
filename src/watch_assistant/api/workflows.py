"""Authenticated workflow aggregation routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from watch_assistant.schemas import (
    MediaType,
    WorkflowApprovalRequest,
    WorkflowCancelRequest,
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowStageName,
    WorkflowStagePatch,
    WorkflowStageStatus,
    WorkflowStatus,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.workflows import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_workflow_service(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflow_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="workflows_unavailable")
    return service


WorkflowServiceDependency = Annotated[WorkflowService, Depends(get_workflow_service)]


@router.post(
    "/workflows", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED
)
async def create_workflow(
    payload: WorkflowCreateRequest,
    service: WorkflowServiceDependency,
) -> WorkflowResponse:
    return await service.create(payload)


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(
    service: WorkflowServiceDependency,
    workflow_status: Annotated[
        WorkflowStatus | None, Query(alias="status")
    ] = None,
    media_type: MediaType | None = None,
    subscription_id: Annotated[str | None, Query(max_length=64)] = None,
    stage: WorkflowStageName | None = None,
    stage_status: WorkflowStageStatus | None = None,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WorkflowListResponse:
    return await service.list(
        status=workflow_status,
        media_type=media_type,
        subscription_id=subscription_id,
        stage=stage,
        stage_status=stage_status,
        page=page,
        page_size=page_size,
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str, service: WorkflowServiceDependency
) -> WorkflowResponse:
    try:
        return await service.get(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc


@router.patch(
    "/workflows/{workflow_id}/stages/{stage_name}", response_model=WorkflowResponse
)
async def patch_workflow_stage(
    workflow_id: str,
    stage_name: WorkflowStageName,
    payload: WorkflowStagePatch,
    service: WorkflowServiceDependency,
) -> WorkflowResponse:
    try:
        return await service.patch_stage(workflow_id, stage_name, payload)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code not in {
            "workflow_prerequisite_not_met",
            "workflow_stage_regression",
            "workflow_stage_terminal",
        }:
            code = "workflow_conflict"
        raise HTTPException(status_code=409, detail=code) from None


@router.post(
    "/workflows/{workflow_id}/approval", response_model=WorkflowResponse
)
async def decide_workflow_approval(
    workflow_id: str,
    payload: WorkflowApprovalRequest,
    service: WorkflowServiceDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> WorkflowResponse:
    try:
        return await service.decide_approval(
            workflow_id, payload, actor_id=auth.identity
        )
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code not in {"workflow_not_awaiting_confirmation", "workflow_not_cancellable"}:
            code = "workflow_conflict"
        raise HTTPException(status_code=409, detail=code) from None


@router.post("/workflows/{workflow_id}/cancel", response_model=WorkflowResponse)
async def cancel_workflow(
    workflow_id: str,
    payload: WorkflowCancelRequest,
    service: WorkflowServiceDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> WorkflowResponse:
    try:
        return await service.cancel(workflow_id, payload, actor_id=auth.identity)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code != "workflow_not_cancellable":
            code = "workflow_conflict"
        raise HTTPException(status_code=409, detail=code) from None
