"""Web management routes for outbound Webhook subscriptions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.api.agent import require_web_auth
from watch_assistant.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookEndpointCreateRequest,
    WebhookEndpointCreateResponse,
    WebhookEndpointListResponse,
    WebhookEndpointPatch,
    WebhookEndpointResponse,
)
from watch_assistant.security import AuthContext
from watch_assistant.services.webhooks import WebhookError, WebhookService

router = APIRouter(prefix="/api/v1/webhooks")


def get_service(request: Request) -> WebhookService:
    service = getattr(request.app.state, "webhook_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="webhooks_unavailable")
    return service


ServiceDependency = Annotated[WebhookService, Depends(get_service)]
WebAuthDependency = Annotated[AuthContext, Depends(require_web_auth)]


@router.post("", response_model=WebhookEndpointCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: WebhookEndpointCreateRequest,
    _: WebAuthDependency,
    service: ServiceDependency,
) -> WebhookEndpointCreateResponse:
    try:
        return await service.create(payload)
    except WebhookError as exc:
        raise _http_error(exc) from None


@router.get("", response_model=WebhookEndpointListResponse)
async def list_webhooks(
    _: WebAuthDependency, service: ServiceDependency
) -> WebhookEndpointListResponse:
    return await service.list()


@router.patch("/{endpoint_id}", response_model=WebhookEndpointResponse)
async def patch_webhook(
    endpoint_id: str,
    payload: WebhookEndpointPatch,
    _: WebAuthDependency,
    service: ServiceDependency,
) -> WebhookEndpointResponse:
    try:
        return await service.patch(endpoint_id, payload)
    except WebhookError as exc:
        raise _http_error(exc) from None


@router.post("/{endpoint_id}/rotate", response_model=WebhookEndpointCreateResponse)
async def rotate_webhook(
    endpoint_id: str, _: WebAuthDependency, service: ServiceDependency
) -> WebhookEndpointCreateResponse:
    try:
        return await service.rotate(endpoint_id)
    except WebhookError as exc:
        raise _http_error(exc) from None


@router.post("/{endpoint_id}/test", response_model=WebhookDeliveryResponse)
async def test_webhook(
    endpoint_id: str, _: WebAuthDependency, service: ServiceDependency
) -> WebhookDeliveryResponse:
    try:
        return await service.enqueue_test(endpoint_id)
    except WebhookError as exc:
        raise _http_error(exc) from None


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    endpoint_id: str, _: WebAuthDependency, service: ServiceDependency
) -> None:
    try:
        await service.delete(endpoint_id)
    except WebhookError as exc:
        raise _http_error(exc) from None


@router.get("/deliveries", response_model=WebhookDeliveryListResponse)
async def list_deliveries(
    _: WebAuthDependency, service: ServiceDependency
) -> WebhookDeliveryListResponse:
    return await service.deliveries()


@router.post("/publish-due")
async def publish_due(
    _: WebAuthDependency, service: ServiceDependency
) -> dict[str, int]:
    return {"delivered_count": await service.publish_due()}


@router.post("/deliveries/{delivery_id}/retry", response_model=WebhookDeliveryResponse)
async def retry_delivery(
    delivery_id: str, _: WebAuthDependency, service: ServiceDependency
) -> WebhookDeliveryResponse:
    try:
        return await service.retry_dead(delivery_id)
    except WebhookError as exc:
        raise _http_error(exc) from None


def _http_error(error: WebhookError) -> HTTPException:
    status_code = {
        "webhook_not_found": 404,
        "webhook_conflict": 409,
        "webhook_url_not_allowed": 422,
        "webhook_event_not_allowed": 422,
        "webhook_delivery_not_found": 404,
        "webhook_delivery_conflict": 409,
        "webhook_endpoint_disabled": 409,
        "webhook_dns_failed": 422,
    }.get(error.code, 422)
    return HTTPException(status_code=status_code, detail={"code": error.code})


__all__ = ["router"]
