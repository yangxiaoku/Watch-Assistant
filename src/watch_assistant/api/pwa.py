"""Authenticated PWA device registration and revocation routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import (
    PwaDeviceListResponse,
    PwaDeviceRegisterRequest,
    PwaDeviceResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.pwa_devices import PwaDeviceError, PwaDeviceService

router = APIRouter(prefix="/api/v1/pwa")
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def get_service(request: Request) -> PwaDeviceService:
    service = getattr(request.app.state, "pwa_device_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="pwa_unavailable")
    return service


@router.get("/devices", response_model=PwaDeviceListResponse)
async def list_devices(context: AuthDependency, request: Request) -> PwaDeviceListResponse:
    return await get_service(request).list(_owner_identity(context))


@router.post("/devices", response_model=PwaDeviceResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    payload: PwaDeviceRegisterRequest, context: AuthDependency, request: Request
) -> PwaDeviceResponse:
    try:
        return await get_service(request).register(_owner_identity(context), payload)
    except PwaDeviceError as exc:
        raise _http_error(exc) from None


@router.post("/devices/{device_id}/revoke", response_model=PwaDeviceResponse)
async def revoke_device(
    device_id: str, context: AuthDependency, request: Request
) -> PwaDeviceResponse:
    try:
        return await get_service(request).revoke(_owner_identity(context), device_id)
    except PwaDeviceError as exc:
        raise _http_error(exc) from None


def _http_error(error: PwaDeviceError) -> HTTPException:
    # L2: pwa_device_conflict 是设备绑定冲突,应 409;not_found 才 404。
    if error.code == "pwa_device_not_found":
        status_code = 404
    elif error.code == "pwa_device_conflict":
        status_code = 409
    else:
        status_code = 422
    return HTTPException(status_code=status_code, detail={"code": error.code})


def _owner_identity(context: AuthContext) -> str:
    # Web sessions rotate; all web sessions belong to the one local account.
    return "web-account" if not context.via_bearer else context.identity


__all__ = ["router"]
