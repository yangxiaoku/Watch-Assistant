"""Authenticated in-app notification routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from watch_assistant.schemas import (
    NotificationListResponse,
    NotificationPreferencePatch,
    NotificationPreferenceResponse,
    NotificationResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.notifications import (
    NotificationConflict,
    NotificationNotFound,
    NotificationService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_notification_service(request: Request) -> NotificationService:
    service = getattr(request.app.state, "notification_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="notifications_unavailable")
    return service


ServiceDependency = Annotated[NotificationService, Depends(get_notification_service)]


@router.get("/notifications", response_model=NotificationListResponse)
async def list_notifications(
    service: ServiceDependency,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotificationListResponse:
    return await service.list(unread_only=unread_only, limit=limit)


@router.post("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: str, service: ServiceDependency
) -> NotificationResponse:
    try:
        return await service.mark_read(notification_id)
    except NotificationNotFound as exc:
        raise HTTPException(status_code=404, detail="notification_not_found") from exc


@router.post("/notifications/read-all")
async def mark_all_notifications_read(service: ServiceDependency) -> dict[str, int]:
    return {"marked_count": await service.mark_all_read()}


@router.get(
    "/notification-preferences", response_model=NotificationPreferenceResponse
)
async def get_notification_preferences(
    service: ServiceDependency,
) -> NotificationPreferenceResponse:
    return await service.get_preferences()


@router.patch(
    "/notification-preferences", response_model=NotificationPreferenceResponse
)
async def update_notification_preferences(
    payload: NotificationPreferencePatch,
    service: ServiceDependency,
) -> NotificationPreferenceResponse:
    try:
        return await service.update_preferences(payload)
    except NotificationConflict:
        raise HTTPException(
            status_code=409, detail="notification_preference_conflict"
        ) from None
