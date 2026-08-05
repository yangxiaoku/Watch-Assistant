"""Authenticated, local-only organization plan review routes."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.schemas import (
    OrganizationPlanAliasRequest,
    OrganizationPlanCandidateRequest,
    OrganizationPlanCandidateSearchRequest,
    OrganizationPlanListResponse,
    OrganizationPlanMutationRequest,
    OrganizationPlanResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanService,
    OrganizationPlanStatus,
)


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
    "/organization-plans/{plan_id}/confirm", response_model=OrganizationPlanResponse
)
async def confirm_organization_plan(
    plan_id: str,
    payload: OrganizationPlanMutationRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        if payload.plan_hash is not None:
            current = await service.get_plan(plan_id)
            if not hmac.compare_digest(current.plan_hash, payload.plan_hash):
                raise OrganizationPlanError("plan_hash_mismatch")
        item = await service.confirm_plan(
            plan_id, expected_revision=payload.expected_revision
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


@router.post(
    "/organization-plans/{plan_id}/candidate", response_model=OrganizationPlanResponse
)
async def select_organization_candidate(
    plan_id: str,
    payload: OrganizationPlanCandidateRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        item = await service.select_candidate(
            plan_id,
            source_object_id=payload.source_object_id,
            tmdb_id=payload.tmdb_id,
            expected_revision=payload.expected_revision,
        )
    except OrganizationPlanError as exc:
        raise _http_error(exc) from None
    return OrganizationPlanResponse.model_validate(item.to_public_dict())


@router.post(
    "/organization-plans/{plan_id}/candidate-search",
    response_model=OrganizationPlanResponse,
)
async def search_organization_candidates(
    plan_id: str,
    payload: OrganizationPlanCandidateSearchRequest,
    service: ServiceDependency,
) -> OrganizationPlanResponse:
    try:
        item = await service.search_candidates(
            plan_id,
            expected_revision=payload.expected_revision,
            source_object_id=payload.source_object_id,
            source_index=payload.source_index,
            query=payload.query,
            limit=payload.limit,
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
        "plan_hash_mismatch": 409,
        "plan_not_reviewable": 409,
        "plan_expired": 409,
        "plan_prerequisites_changed": 409,
        "plan_not_executable": 409,
        "invalid_source_object": 422,
        "invalid_tmdb_candidate": 422,
        "tmdb_candidate_not_found": 404,
        "candidate_target_unavailable": 409,
        "source_snapshot_mismatch": 409,
        "candidate_source_required": 422,
        "invalid_source_index": 422,
        "invalid_candidate_query": 422,
        "invalid_candidate_limit": 422,
        "candidate_search_unavailable": 503,
    }
    messages = {
        "plan_not_found": "计划不存在",
        "invalid_plan": "计划标识无效",
        "invalid_pagination": "分页参数无效",
        "invalid_revision": "计划版本无效",
        "invalid_alias": "别名格式不受支持",
        "stale_revision": "计划版本已变化，请刷新后重试",
        "plan_hash_mismatch": "计划摘要已变化，请刷新计划后重新确认",
        "plan_not_reviewable": "计划当前状态不可修改",
        "plan_expired": "整理计划已过期，请重新生成预览",
        "plan_prerequisites_changed": "计划前置证据已变化，请重新扫描并生成计划",
        "plan_not_executable": "计划缺少完整执行载荷，请重新生成预览",
        "invalid_source_object": "来源影片标识无效",
        "invalid_tmdb_candidate": "TMDB 候选无效",
        "tmdb_candidate_not_found": "TMDB 候选不存在，请刷新后重试",
        "candidate_target_unavailable": "候选影片无法生成安全归档路径",
        "source_snapshot_mismatch": "来源扫描快照已变化，请重新扫描",
        "candidate_source_required": "请先选择要搜索的来源条目",
        "invalid_source_index": "来源条目标识无效",
        "invalid_candidate_query": "搜索关键词无效",
        "invalid_candidate_limit": "候选数量限制无效",
        "candidate_search_unavailable": "TMDB 候选搜索暂不可用，请稍后重试",
    }
    code = error.code if error.code in messages else "organization_plan_unavailable"
    status = statuses.get(code, 409)
    message = messages.get(code, "计划暂不可用")
    return HTTPException(status_code=status, detail={"code": code, "message": message})


__all__ = ["router"]
