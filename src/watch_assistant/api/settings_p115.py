"""Authenticated P115 settings and read-only validation routes."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.p115_settings import (
    P115SettingsService,
    P115ValidationRateLimited,
)

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_auth)],
)


class P115CookieResponse(BaseModel):
    source: Literal["tgtodrive"]
    configured: bool
    structure_valid: bool
    sync_status: Literal["success", "failed", "unknown"]
    last_sync_at: datetime | None


class P115CapabilitiesResponse(BaseModel):
    magnet: bool
    share: bool


class P115SettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    ready: bool
    capabilities: P115CapabilitiesResponse
    cookie: P115CookieResponse
    target_configured: bool
    max_concurrency: int


class P115ValidationResponse(BaseModel):
    status: Literal["ready", "needs_auth", "unavailable"]
    checked_at: datetime


def get_p115_settings_service(request: Request) -> P115SettingsService:
    service = getattr(request.app.state, "p115_settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="p115_settings_unavailable")
    return service


P115SettingsDependency = Annotated[
    P115SettingsService, Depends(get_p115_settings_service)
]


async def require_p115_validation_access(
    auth: Annotated[AuthContext, Depends(require_api_auth)],
    service: P115SettingsDependency,
) -> None:
    try:
        service.check_validation_rate_limit(auth.identity)
    except P115ValidationRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limited"
        ) from exc


@router.get("/settings/p115", response_model=P115SettingsResponse)
async def get_p115_settings(
    request: Request,
    service: P115SettingsDependency,
) -> P115SettingsResponse:
    state = request.app.state
    runtime_ready = getattr(state, "p115_ready", None) is True
    capabilities = getattr(state, "push_capabilities", None)
    runtime_magnet_capability = (
        isinstance(capabilities, Mapping)
        and type(capabilities.get("magnet")) is bool
        and type(capabilities.get("share")) is bool
        and capabilities.get("magnet") is True
    )
    return P115SettingsResponse.model_validate(
        service.snapshot(
            runtime_ready=runtime_ready,
            runtime_magnet_capability=runtime_magnet_capability,
        ),
        from_attributes=True,
    )


@router.post(
    "/settings/p115/validate",
    response_model=P115ValidationResponse,
)
async def validate_p115(
    service: P115SettingsDependency,
    _: Annotated[None, Depends(require_p115_validation_access)],
) -> P115ValidationResponse:
    result = await service.validate()
    return P115ValidationResponse.model_validate(result, from_attributes=True)
