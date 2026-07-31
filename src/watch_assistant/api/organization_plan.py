"""Authenticated, local-only organization plan review routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.schemas import (
    OrganizationApprovalWorkflowRequest,
    OrganizationPlanAliasRequest,
    OrganizationPlanListResponse,
    OrganizationPlanMutationRequest,
    OrganizationPlanResponse,
    WorkflowResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanService,
    OrganizationPlanStatus,
)
from watch_assistant.services.workflows import WorkflowConflict, WorkflowService


async def require_organization_plan_enabled(request: Request) -> None:
    if not getattr(request.app.state, "organization_plan_enabled", False):
        raise HTTPException(status_code=503, detail="organization_plan_disabled")


router = APIRouter(
    prefix="/api/v1",
    dependencies=[
        Depends(require_api_auth),
        Depends(require_organization_plan_enabled),
    ],
)


def get_organization_plan_service(request: Request) -> OrganizationPlanService:
    service = getattr(request.app.state, "organization_plan_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="organization_plan_unavailable")
    return service


ServiceDependency = Annotated[
    OrganizationPlanService, Depends(get_organization_plan_service)
]


def get_workflow_service(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflow_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="workflows_unavailable")
    return service


WorkflowServiceDependency = Annotated[WorkflowService, Depends(get_workflow_service)]


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


WebAuthDependency = Annotated[AuthContext, Depends(require_web_auth)]


@router.get("/organization-plans", response_model=OrganizationPlanListResponse)
async def list_organization_plans(
    service: ServiceDependency,
    status: Annotated[OrganizationPlanStatus | None, Query()] = None,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OrganizationPlanListResponse:
    try:
        items, next_cursor = await service.list_plans(
            status=status, cursor=cursor, limit=limit
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanListResponse(
        items=[
            OrganizationPlanResponse.model_validate(item.to_public_dict())
            for item in items
        ],
        next_cursor=next_cursor,
    )


@router.get("/organization-plans/{plan_id}", response_model=OrganizationPlanResponse)
async def get_organization_plan(
    plan_id: str, service: ServiceDependency
) -> OrganizationPlanResponse:
    try:
        item = await service.get_plan(plan_id)
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


@router.post(
    "/organization-plans/{plan_id}/approval-workflow",
    response_model=WorkflowResponse,
)
async def create_organization_plan_approval_workflow(
    plan_id: str,
    payload: OrganizationApprovalWorkflowRequest,
    _: WebAuthDependency,
    service: WorkflowServiceDependency,
) -> WorkflowResponse:
    try:
        return await service.create_organization_plan_approval(
            plan_id, expected_revision=payload.expected_revision
        )
    except WorkflowConflict as exc:
        raise _http_workflow_error(exc) from None


@router.post(
    "/organization-plans/{plan_id}/confirm", response_model=OrganizationPlanResponse
)
async def confirm_organization_plan(
    plan_id: str,
    payload: OrganizationPlanMutationRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        item = await service.confirm_plan(
            plan_id, expected_revision=payload.expected_revision
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


@router.post(
    "/organization-plans/{plan_id}/ignore", response_model=OrganizationPlanResponse
)
async def ignore_organization_plan(
    plan_id: str,
    payload: OrganizationPlanMutationRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        item = await service.ignore_plan_at_revision(
            plan_id, expected_revision=payload.expected_revision
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


@router.post(
    "/organization-plans/{plan_id}/alias", response_model=OrganizationPlanResponse
)
async def alias_organization_plan(
    plan_id: str,
    payload: OrganizationPlanAliasRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        item = await service.alias_plan(
            plan_id,
            alias=payload.alias,
            expected_revision=payload.expected_revision,
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


def _http_error(error: OrganizationPlanError) -> HTTPException:
    statuses = {
        "plan_not_found": 404,
        "invalid_plan": 422,
        "invalid_pagination": 422,
        "invalid_revision": 422,
        "invalid_alias": 422,
        "stale_revision": 409,
        "plan_not_reviewable": 409,
    }
    messages = {
        "plan_not_found": "计划不存在",
        "invalid_plan": "计划标识无效",
        "invalid_pagination": "分页参数无效",
        "invalid_revision": "计划版本无效",
        "invalid_alias": "别名格式不受支持",
        "stale_revision": "计划版本已变化，请刷新后重试",
        "plan_not_reviewable": "计划当前状态不可修改",
    }
    code = error.code if error.code in messages else "organization_plan_unavailable"
    status = statuses.get(code, 409)
    message = messages.get(code, "计划暂不可用")
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _http_workflow_error(error: WorkflowConflict) -> HTTPException:
    statuses = {
        "organization_plan_not_found": 404,
        "organization_plan_revision_changed": 409,
        "organization_plan_not_planned": 409,
    }
    messages = {
        "organization_plan_not_found": "整理计划不存在",
        "organization_plan_revision_changed": "计划版本已变化，请刷新后重试",
        "organization_plan_not_planned": "计划尚未确认或已不可用",
    }
    code = str(error)
    return HTTPException(
        status_code=statuses.get(code, 409),
        detail={
            "code": code if code in messages else "workflow_unavailable",
            "message": messages.get(code, "审批工作流暂不可用"),
        },
    )


__all__ = ["router"]
