"""Authenticated routes for completed organization history."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.api.resource_scope import scoped_library_ids
from watch_assistant.schemas import (
    OrganizationHistoryListResponse,
    OrganizationHistoryResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.organization_history import (
    OrganizationHistoryError,
    OrganizationHistoryService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_service(request: Request) -> OrganizationHistoryService:
    service = getattr(request.app.state, "organization_history_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="organization_history_unavailable")
    return service


ServiceDependency = Annotated[OrganizationHistoryService, Depends(get_service)]
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


@router.get("/organization-history", response_model=OrganizationHistoryListResponse)
async def list_organization_history(
    service: ServiceDependency,
    context: AuthDependency,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OrganizationHistoryListResponse:
    try:
        items, next_cursor = await service.list_items(
            cursor=cursor,
            limit=limit,
            library_ids=scoped_library_ids(context),
        )
    except OrganizationHistoryError as exc:
        raise _http_error(exc) from None
    return OrganizationHistoryListResponse(
        items=[OrganizationHistoryResponse.model_validate(item.to_public_dict()) for item in items],
        next_cursor=next_cursor,
    )


@router.get("/organization-history/{item_id}", response_model=OrganizationHistoryResponse)
async def get_organization_history(
    item_id: str,
    service: ServiceDependency,
    context: AuthDependency,
) -> OrganizationHistoryResponse:
    try:
        item = await service.get(
            item_id,
            library_ids=scoped_library_ids(context),
        )
    except OrganizationHistoryError as exc:
        raise _http_error(exc) from None
    return OrganizationHistoryResponse.model_validate(item.to_public_dict())


def _http_error(error: OrganizationHistoryError) -> HTTPException:
    status = {"history_not_found": 404}.get(error.code, 422)
    message = {
        "history_not_found": "整理历史不存在",
        "invalid_history_id": "整理历史标识无效",
        "invalid_pagination": "分页参数无效",
    }.get(error.code, "整理历史暂不可用")
    return HTTPException(status_code=status, detail={"code": error.code, "message": message})


__all__ = ["router"]
