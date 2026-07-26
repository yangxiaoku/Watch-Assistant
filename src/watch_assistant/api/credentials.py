"""Authenticated managed credential endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    CredentialRequest,
    CredentialResetRequest,
    CredentialSettingsResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.credentials import (
    CredentialConflict,
    CredentialRateLimited,
    CredentialRejected,
    CredentialService,
    CredentialValidationUnavailable,
)

router = APIRouter(prefix="/api/v1")


def get_credential_service(request: Request) -> CredentialService:
    service = getattr(request.app.state, "credential_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="credentials_unavailable")
    return service


CredentialDependency = Annotated[CredentialService, Depends(get_credential_service)]
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def _rate_limit(service: CredentialService, auth: AuthContext) -> None:
    try:
        service.check_rate_limit(auth.identity)
    except CredentialRateLimited as exc:
        raise HTTPException(status_code=429, detail="rate_limited") from exc


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, CredentialConflict):
        return HTTPException(status_code=409, detail="settings_conflict")
    if isinstance(error, CredentialRejected):
        return HTTPException(status_code=422, detail="credential_rejected")
    if isinstance(error, CredentialValidationUnavailable):
        return HTTPException(
            status_code=503, detail="credential_validation_unavailable"
        )
    return HTTPException(status_code=503, detail="credential_unavailable")


@router.get("/settings/credentials", response_model=CredentialSettingsResponse)
async def get_credentials(
    service: CredentialDependency, _auth: AuthDependency
) -> CredentialSettingsResponse:
    return CredentialSettingsResponse.model_validate(await service.snapshot())


@router.put("/settings/credentials/tmdb", response_model=CredentialSettingsResponse)
async def put_tmdb(
    payload: CredentialRequest,
    service: CredentialDependency,
    auth: AuthDependency,
) -> CredentialSettingsResponse:
    _rate_limit(service, auth)
    try:
        result = await service.update_tmdb(
            payload.value.get_secret_value(), payload.revision
        )
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_error(error) from None
    return CredentialSettingsResponse.model_validate(result)


@router.put(
    "/settings/credentials/p115-cookie", response_model=CredentialSettingsResponse
)
async def put_p115(
    payload: CredentialRequest,
    service: CredentialDependency,
    auth: AuthDependency,
) -> CredentialSettingsResponse:
    _rate_limit(service, auth)
    try:
        result = await service.update_p115_cookie(
            payload.value.get_secret_value(), payload.revision
        )
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_error(error) from None
    return CredentialSettingsResponse.model_validate(result)


@router.post(
    "/settings/credentials/tmdb/reset", response_model=CredentialSettingsResponse
)
async def reset_tmdb(
    payload: CredentialResetRequest,
    service: CredentialDependency,
    auth: AuthDependency,
) -> CredentialSettingsResponse:
    _rate_limit(service, auth)
    try:
        result = await service.reset_tmdb(payload.revision)
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_error(error) from None
    return CredentialSettingsResponse.model_validate(result)


@router.post(
    "/settings/credentials/p115-cookie/reset", response_model=CredentialSettingsResponse
)
async def reset_p115(
    payload: CredentialResetRequest,
    service: CredentialDependency,
    auth: AuthDependency,
) -> CredentialSettingsResponse:
    _rate_limit(service, auth)
    try:
        result = await service.reset_p115_cookie(payload.revision)
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_error(error) from None
    return CredentialSettingsResponse.model_validate(result)
