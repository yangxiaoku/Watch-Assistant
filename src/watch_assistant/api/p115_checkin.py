"""Authenticated 115 daily check-in settings and read-only status routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    P115CheckInSettingsPatch,
    P115CheckInSettingsResponse,
    P115CheckInStatusResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.p115_checkin import CheckInUnavailable, P115CheckInService
from watch_assistant.services.settings import SettingsService

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_auth)],
)


def get_settings_service(request: Request) -> SettingsService:
    service = getattr(request.app.state, "settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="settings_unavailable")
    return service


SettingsDependency = Annotated[SettingsService, Depends(get_settings_service)]


def get_checkin_service(request: Request) -> P115CheckInService | None:
    """Return the check-in service or None so status can fail closed to 200."""
    return getattr(request.app.state, "p115_checkin_service", None)


@router.get("/settings/p115-checkin", response_model=P115CheckInSettingsResponse)
async def get_p115_checkin(
    settings: SettingsDependency,
) -> P115CheckInSettingsResponse:
    return await settings.get_p115_checkin()


@router.patch("/settings/p115-checkin", response_model=P115CheckInSettingsResponse)
async def patch_p115_checkin(
    patch: P115CheckInSettingsPatch,
    settings: SettingsDependency,
) -> P115CheckInSettingsResponse:
    await settings.set_p115_checkin(patch.enabled, patch.check_in_time)
    return await settings.get_p115_checkin()


@router.get("/p115-checkin/status", response_model=P115CheckInStatusResponse)
async def get_p115_checkin_status(
    request: Request,
    settings: SettingsDependency,
) -> P115CheckInStatusResponse:
    """Return the daily sign-in status read-only; remote errors stay 200.

    The effective toggle is the user-stored value OR the deployment env
    default (P115_CHECK_IN_ENABLED, mirrored onto app.state at startup).
    When the effective toggle is off we must not make any 115 network call,
    so the endpoint fails closed to the disabled shape without touching the
    service.
    """
    stored = await settings.get_p115_checkin()
    env_enabled = bool(getattr(request.app.state, "p115_check_in_enabled", False))
    effective_enabled = stored.enabled or env_enabled
    if not effective_enabled:
        return P115CheckInStatusResponse(
            enabled=False,
            is_sign_today=False,
            continuous_day=0,
            points_num="",
            error_code=None,
        )
    service = get_checkin_service(request)
    if service is None:
        return P115CheckInStatusResponse(
            enabled=stored.enabled,
            is_sign_today=False,
            error_code="checkin_service_unavailable",
        )
    try:
        status = await service.status()
    except CheckInUnavailable as exc:
        return P115CheckInStatusResponse(
            enabled=stored.enabled,
            is_sign_today=False,
            error_code=exc.code,
        )
    return P115CheckInStatusResponse(
        enabled=stored.enabled,
        is_sign_today=bool(status.get("is_sign_today", False)),
        continuous_day=int(status.get("continuous_day") or 0),
        points_num=str(status.get("points_num") or ""),
    )
