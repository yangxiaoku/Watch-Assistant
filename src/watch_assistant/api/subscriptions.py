"""Authenticated subscription management routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from watch_assistant.schemas import (
    SubscriptionCheckResponse,
    SubscriptionCreateRequest,
    SubscriptionMutationRequest,
    SubscriptionResourceObservationResponse,
    SubscriptionResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.subscriptions import (
    SubscriptionConflict,
    SubscriptionNotFound,
    SubscriptionService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_subscription_service(request: Request) -> SubscriptionService:
    service = getattr(request.app.state, "subscription_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="subscription_unavailable")
    return service


SubscriptionServiceDependency = Annotated[
    SubscriptionService, Depends(get_subscription_service)
]


@router.post(
    "/subscriptions",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    payload: SubscriptionCreateRequest,
    service: SubscriptionServiceDependency,
) -> SubscriptionResponse:
    try:
        return await service.create(payload)
    except SubscriptionConflict as exc:
        code = str(exc)
        if code not in {"subscription_exists", "subscription_conflict"}:
            code = "subscription_conflict"
        raise HTTPException(status_code=409, detail=code) from None


@router.get("/subscriptions", response_model=list[SubscriptionResponse])
async def list_subscriptions(
    service: SubscriptionServiceDependency,
) -> list[SubscriptionResponse]:
    return await service.list()


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
async def get_subscription(
    subscription_id: str,
    service: SubscriptionServiceDependency,
) -> SubscriptionResponse:
    try:
        return await service.get(subscription_id)
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail="subscription_not_found") from exc


@router.get(
    "/subscriptions/{subscription_id}/observations",
    response_model=list[SubscriptionResourceObservationResponse],
)
async def list_subscription_observations(
    subscription_id: str,
    service: SubscriptionServiceDependency,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[SubscriptionResourceObservationResponse]:
    try:
        return await service.list_observations(subscription_id, limit=limit)
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail="subscription_not_found") from exc
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid_pagination") from None


@router.post(
    "/subscriptions/{subscription_id}/check",
    response_model=SubscriptionCheckResponse,
)
async def check_subscription(
    subscription_id: str,
    service: SubscriptionServiceDependency,
) -> SubscriptionCheckResponse:
    try:
        return await service.check(subscription_id)
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail="subscription_not_found") from exc
    except SubscriptionConflict as exc:
        code = str(exc)
        if code not in {"subscription_conflict", "subscription_not_active", "subscription_cancelled", "subscription_not_pauseable", "subscription_check_failed"}:
            code = "subscription_conflict"
        raise HTTPException(status_code=409, detail=code) from None


async def _mutate(
    subscription_id: str,
    payload: SubscriptionMutationRequest,
    service: SubscriptionService,
    action: str,
) -> SubscriptionResponse:
    try:
        return await service.mutate(subscription_id, payload, action)
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail="subscription_not_found") from exc
    except SubscriptionConflict as exc:
        code = str(exc)
        if code not in {"subscription_conflict", "subscription_not_active", "subscription_cancelled", "subscription_not_pauseable", "subscription_check_failed"}:
            code = "subscription_conflict"
        raise HTTPException(status_code=409, detail=code) from None


@router.post(
    "/subscriptions/{subscription_id}/pause", response_model=SubscriptionResponse
)
async def pause_subscription(
    subscription_id: str,
    payload: SubscriptionMutationRequest,
    service: SubscriptionServiceDependency,
) -> SubscriptionResponse:
    return await _mutate(subscription_id, payload, service, "pause")


@router.post(
    "/subscriptions/{subscription_id}/resume", response_model=SubscriptionResponse
)
async def resume_subscription(
    subscription_id: str,
    payload: SubscriptionMutationRequest,
    service: SubscriptionServiceDependency,
) -> SubscriptionResponse:
    return await _mutate(subscription_id, payload, service, "resume")


@router.post(
    "/subscriptions/{subscription_id}/cancel", response_model=SubscriptionResponse
)
async def cancel_subscription(
    subscription_id: str,
    payload: SubscriptionMutationRequest,
    service: SubscriptionServiceDependency,
) -> SubscriptionResponse:
    return await _mutate(subscription_id, payload, service, "cancel")
