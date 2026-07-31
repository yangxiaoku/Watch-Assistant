"""Authenticated workflow aggregation routes."""

from datetime import datetime
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


async def require_web_auth(request: Request) -> AuthContext:
    context = await require_api_auth(request)
    if context.via_bearer and context.identity != "internal":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "web_approval_required",
                "message": "高风险批准必须由 Web 会话完成",
            },
        )
    return context


def get_workflow_service(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflow_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="workflows_unavailable")
    return service


WorkflowServiceDependency = Annotated[WorkflowService, Depends(get_workflow_service)]
WebAuthDependency = Annotated[AuthContext, Depends(require_web_auth)]


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
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WorkflowListResponse:
    if (
        updated_after is not None
        and updated_before is not None
        and updated_after > updated_before
    ):
        raise HTTPException(status_code=422, detail="invalid_request")
    return await service.list(
        status=workflow_status,
        media_type=media_type,
        subscription_id=subscription_id,
        stage=stage,
        stage_status=stage_status,
        updated_after=updated_after,
        updated_before=updated_before,
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
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> WorkflowResponse:
    # Approval must go through the dedicated endpoint so Agent tokens cannot
    # turn a waiting approval into a terminal decision.
    if (
        stage_name is WorkflowStageName.APPROVAL
        and auth.via_bearer
        and payload.status
        in {WorkflowStageStatus.SUCCEEDED, WorkflowStageStatus.CANCELLED}
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "web_approval_required",
                "message": "高风险批准必须由 Web 会话完成",
            },
        )
    try:
        return await service.patch_stage(workflow_id, stage_name, payload)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc


@router.post(
    "/workflows/{workflow_id}/approval", response_model=WorkflowResponse
)
async def decide_workflow_approval(
    workflow_id: str,
    payload: WorkflowApprovalRequest,
    service: WorkflowServiceDependency,
    auth: WebAuthDependency,
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
