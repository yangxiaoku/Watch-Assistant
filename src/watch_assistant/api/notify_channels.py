"""通知渠道管理路由(Web 会话专用,禁 agent token)。"""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.api.agent import require_web_auth
from watch_assistant.schemas import (
    NotifyChannelCreateRequest,
    NotifyChannelListResponse,
    NotifyChannelPatch,
    NotifyChannelResponse,
)
from watch_assistant.security import AuthContext
from watch_assistant.services.notify_channels_service import (
    NotifyChannelError,
    NotifyChannelService,
)

router = APIRouter(prefix="/api/v1/notify-channels")


def get_service(request: Request) -> NotifyChannelService:
    service = getattr(request.app.state, "notify_channel_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="notify_channels_unavailable")
    return service


ServiceDependency = Annotated[NotifyChannelService, Depends(get_service)]
WebAuthDependency = Annotated[AuthContext, Depends(require_web_auth)]


def _response(item) -> NotifyChannelResponse:
    return NotifyChannelResponse(**asdict(item))


@router.post("", response_model=NotifyChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: NotifyChannelCreateRequest,
    _: WebAuthDependency,
    service: ServiceDependency,
) -> NotifyChannelResponse:
    try:
        return _response(
            await service.create(
                payload.name,
                payload.webhook_url,
                kind=payload.kind,
                target=payload.target,
                cli_channel=payload.cli_channel,
                cli_account=payload.cli_account,
            )
        )
    except NotifyChannelError as exc:
        raise _http_error(exc) from None


@router.get("", response_model=NotifyChannelListResponse)
async def list_channels(
    _: WebAuthDependency, service: ServiceDependency
) -> NotifyChannelListResponse:
    return NotifyChannelListResponse(items=[_response(item) for item in await service.list()])


@router.patch("/{channel_id}", response_model=NotifyChannelResponse)
async def set_channel_enabled(
    channel_id: str,
    payload: NotifyChannelPatch,
    _: WebAuthDependency,
    service: ServiceDependency,
) -> NotifyChannelResponse:
    try:
        return _response(await service.set_enabled(channel_id, payload.enabled))
    except NotifyChannelError as exc:
        raise _http_error(exc) from None


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: str, _: WebAuthDependency, service: ServiceDependency
) -> None:
    try:
        await service.delete(channel_id)
    except NotifyChannelError as exc:
        raise _http_error(exc) from None


def _http_error(error: NotifyChannelError) -> HTTPException:
    status_code = {
        "unsupported_notify_kind": 422,
        "notify_channel_not_found": 404,
        "webhook_url_required": 422,
        "invalid_notify_cli_target": 422,
        "unsupported_feishu_target": 422,
        "invalid_notify_cli_channel": 422,
        "invalid_notify_channel_name": 422,
    }.get(error.code, 422)
    return HTTPException(status_code=status_code, detail={"code": error.code})


__all__ = ["router"]
