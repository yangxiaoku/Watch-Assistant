"""Authenticated P115 settings and read-only validation routes."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.p115_device_types import P115DeviceCode
from watch_assistant.services.p115_login_devices import (
    P115LoginDeviceError,
    P115LoginDeviceService,
)
from watch_assistant.services.p115_qrcode import P115QrcodeError, P115QrcodeService
from watch_assistant.services.p115_settings import (
    P115SettingsService,
    P115ValidationRateLimited,
)

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_auth)],
)

P115_DIRECTORY_ROOT_ID = "0"


class P115CookieResponse(BaseModel):
    source: Literal["managed", "file"]
    configured: bool
    structure_valid: bool


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


class P115DirectoryResponse(BaseModel):
    id: str
    name: str


class P115DirectoryListResponse(BaseModel):
    root_id: str
    parent_id: str
    items: list[P115DirectoryResponse]
    has_more: bool
    next_page: int | None


class P115LoginDeviceResponse(BaseModel):
    id: str
    name: str
    device_code: str
    active: bool
    created_at: datetime
    last_used_at: datetime | None


class P115LoginDeviceListResponse(BaseModel):
    items: list[P115LoginDeviceResponse]


class P115QrcodeCreateRequest(BaseModel):
    device_code: P115DeviceCode = "web"
    device_name: str = "扫码设备"


class P115QrcodeCreateResponse(BaseModel):
    session_id: str
    image_data_url: str
    expires_at: datetime
    status: Literal["waiting"]


class P115QrcodeStatusResponse(BaseModel):
    session_id: str
    status: Literal["waiting", "scanned", "ready", "expired"]
    device: P115LoginDeviceResponse | None = None


class P115LoginDeviceMutationRequest(BaseModel):
    revision: int


def _device_service(request: Request) -> P115LoginDeviceService:
    service = getattr(request.app.state, "p115_login_device_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="p115_device_unavailable")
    return service


def _qrcode_service(request: Request) -> P115QrcodeService:
    service = getattr(request.app.state, "p115_qrcode_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="p115_qrcode_unavailable")
    return service


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


@router.get("/settings/p115/directories", response_model=P115DirectoryListResponse)
async def list_p115_directories(
    request: Request,
    directory_id: str | None = None,
    page: int = 1,
) -> P115DirectoryListResponse:
    """Return bounded child directories from the actual 115 account root."""
    root_id = getattr(
        request.app.state, "p115_directory_picker_root_id", P115_DIRECTORY_ROOT_ID
    )
    if root_id != P115_DIRECTORY_ROOT_ID:
        raise HTTPException(status_code=503, detail="p115_directory_scope_unavailable")
    parent_id = directory_id or root_id
    if not isinstance(parent_id, str) or not parent_id.isdigit():
        raise HTTPException(status_code=503, detail="p115_directory_scope_unavailable")
    allowed_ids = getattr(request.app.state, "p115_browsed_directory_ids", set())
    if parent_id != root_id and parent_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="p115_directory_out_of_scope")
    if page < 1 or page > 100:
        raise HTTPException(status_code=422, detail="invalid_page")
    provider = getattr(request.app.state, "organization_cookie_provider", None)
    if provider is None:
        raise HTTPException(status_code=503, detail="p115_directory_unavailable")
    from watch_assistant.adapters.p115_library_gateway import (
        P115ReadOnlyDirectoryGateway,
        P115ReadOnlyGatewayError,
    )

    gateway = P115ReadOnlyDirectoryGateway(
        provider,
        authorized_directory_ids=tuple(dict.fromkeys((root_id, parent_id))),
        request_timeout_seconds=30,
        allow_virtual_root=True,
    )
    try:
        # The verified transport contract intentionally uses one record per request.
        # Aggregate a bounded page for the browser without exposing files or secrets.
        first_offset = (page - 1) * 50
        directories = []
        current_page = first_offset + 1
        for _ in range(50):
            result = await gateway.list_directory(parent_id, page=current_page, page_size=1)
            for item in result.items:
                if item.is_directory and item.directory_id is not None:
                    directories.append(P115DirectoryResponse(id=item.directory_id, name=item.name))
                    allowed_ids.add(item.directory_id)
            if result.terminal:
                break
            current_page += 1
        return P115DirectoryListResponse(
            root_id=root_id,
            parent_id=parent_id,
            items=directories,
            has_more=not result.terminal,
            next_page=page + 1 if not result.terminal else None,
        )
    except (P115ReadOnlyGatewayError, TimeoutError, OSError):
        raise HTTPException(status_code=503, detail="p115_directory_read_failed") from None


@router.get("/settings/p115/devices", response_model=P115LoginDeviceListResponse)
async def list_p115_devices(request: Request) -> P115LoginDeviceListResponse:
    service = _device_service(request)
    return P115LoginDeviceListResponse(items=await service.list_devices())


@router.post("/settings/p115/qrcode", response_model=P115QrcodeCreateResponse)
async def create_p115_qrcode(
    payload: P115QrcodeCreateRequest, request: Request
) -> P115QrcodeCreateResponse:
    try:
        result = await _qrcode_service(request).create(payload.device_code, payload.device_name)
    except P115QrcodeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return P115QrcodeCreateResponse.model_validate(result)


@router.get(
    "/settings/p115/qrcode/{session_id}", response_model=P115QrcodeStatusResponse
)
async def poll_p115_qrcode(session_id: str, request: Request) -> P115QrcodeStatusResponse:
    try:
        status_value, cookie = await _qrcode_service(request).poll(session_id)
    except P115QrcodeError as exc:
        error_code = str(exc)
        status_code = 404 if error_code == "qrcode_session_not_found" else 503
        raise HTTPException(status_code=status_code, detail=error_code) from None
    device = None
    if cookie is not None:
        credentials = getattr(request.app.state, "credential_service", None)
        if credentials is None:
            raise HTTPException(status_code=503, detail="credentials_unavailable")
        snapshot = await credentials.snapshot()
        try:
            await credentials.update_p115_cookie(
                cookie, int(snapshot["revision"])
            )
            device_code, device_name = await _qrcode_service(request).device_info(session_id)
            device = await _device_service(request).add_device(device_name, device_code, cookie)
            await _qrcode_service(request).consume(session_id)
        except Exception as exc:  # noqa: BLE001 - stable public error only
            del exc
            raise HTTPException(status_code=503, detail="p115_qrcode_save_failed") from None
    return P115QrcodeStatusResponse(
        session_id=session_id,
        status=status_value,
        device=device,
    )


@router.post("/settings/p115/devices/{device_id}/activate", response_model=P115LoginDeviceListResponse)
async def activate_p115_device(
    device_id: str, payload: P115LoginDeviceMutationRequest, request: Request
) -> P115LoginDeviceListResponse:
    credentials = getattr(request.app.state, "credential_service", None)
    if credentials is None:
        raise HTTPException(status_code=503, detail="credentials_unavailable")
    try:
        cookie = await _device_service(request).get_cookie(device_id)
        await credentials.update_p115_cookie(cookie, payload.revision)
        await _device_service(request).mark_active(device_id)
    except P115LoginDeviceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - stable public error only
        del exc
        raise HTTPException(status_code=409, detail="settings_conflict") from None
    return P115LoginDeviceListResponse(items=await _device_service(request).list_devices())


@router.delete("/settings/p115/devices/{device_id}", status_code=204)
async def revoke_p115_device(device_id: str, request: Request) -> None:
    try:
        await _device_service(request).revoke(device_id)
    except P115LoginDeviceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
