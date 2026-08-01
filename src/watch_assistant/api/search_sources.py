"""Managed search-source configuration and read-only connectivity checks."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    CredentialResetRequest,
    ProwlarrSettingsPatch,
    ProwlarrSettingsResponse,
    ProwlarrVerifyResponse,
    SearchSourcesResponse,
    SearchSourceStateResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.prowlarr_settings import (
    ProwlarrSettingsConflict,
    ProwlarrSettingsRateLimited,
    ProwlarrSettingsRejected,
    ProwlarrSettingsService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def get_service(request: Request) -> ProwlarrSettingsService:
    service = getattr(request.app.state, "prowlarr_settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="prowlarr_settings_unavailable")
    return service


ServiceDependency = Annotated[ProwlarrSettingsService, Depends(get_service)]


def _rate_limit(service: ProwlarrSettingsService, auth: AuthContext) -> None:
    try:
        service.check_rate_limit(auth.identity)
    except ProwlarrSettingsRateLimited as exc:
        raise HTTPException(status_code=429, detail="rate_limited") from exc


def _map_settings_error(error: Exception) -> HTTPException:
    if isinstance(error, ProwlarrSettingsConflict):
        return HTTPException(status_code=409, detail="settings_conflict")
    if isinstance(error, ProwlarrSettingsRejected):
        return HTTPException(status_code=422, detail="invalid_prowlarr_settings")
    return HTTPException(status_code=503, detail="prowlarr_settings_unavailable")


@router.get(
    "/settings/search-sources/prowlarr", response_model=ProwlarrSettingsResponse
)
async def get_prowlarr_settings(
    service: ServiceDependency, _auth: AuthDependency
) -> ProwlarrSettingsResponse:
    return ProwlarrSettingsResponse.model_validate(await service.snapshot())


@router.patch(
    "/settings/search-sources/prowlarr", response_model=ProwlarrSettingsResponse
)
async def patch_prowlarr_settings(
    patch: ProwlarrSettingsPatch,
    service: ServiceDependency,
    auth: AuthDependency,
) -> ProwlarrSettingsResponse:
    _rate_limit(service, auth)
    try:
        response = await service.update(
            enabled=patch.enabled,
            base_url=patch.base_url,
            api_key=(
                patch.api_key.get_secret_value()
                if patch.api_key is not None
                else None
            ),
            fields_set=set(patch.model_fields_set) - {"revision"},
            revision=patch.revision,
        )
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_settings_error(error) from None
    return ProwlarrSettingsResponse.model_validate(response)


@router.post(
    "/settings/search-sources/prowlarr/reset",
    response_model=ProwlarrSettingsResponse,
)
async def reset_prowlarr_settings(
    patch: CredentialResetRequest,
    service: ServiceDependency,
    auth: AuthDependency,
) -> ProwlarrSettingsResponse:
    _rate_limit(service, auth)
    try:
        response = await service.reset(patch.revision)
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_settings_error(error) from None
    return ProwlarrSettingsResponse.model_validate(response)


@router.post(
    "/settings/search-sources/prowlarr/verify", response_model=ProwlarrVerifyResponse
)
async def verify_prowlarr(
    service: ServiceDependency, auth: AuthDependency
) -> ProwlarrVerifyResponse:
    _rate_limit(service, auth)
    try:
        response = await service.verify()
    except Exception as error:  # noqa: BLE001 - stable public error mapping
        raise _map_settings_error(error) from None
    return ProwlarrVerifyResponse.model_validate(response)


@router.get("/settings/search-sources", response_model=SearchSourcesResponse)
async def get_search_sources(
    service: ServiceDependency, _auth: AuthDependency
) -> SearchSourcesResponse:
    prowlarr = await service.snapshot()
    prowlarr_status = (
        "configured"
        if prowlarr["configured"]
        else "disabled"
        if not prowlarr["enabled"]
        else "unavailable"
    )
    return SearchSourcesResponse(
        pansou=SearchSourceStateResponse(
            enabled=True,
            configured=True,
            status="configured",
        ),
        prowlarr=SearchSourceStateResponse(
            enabled=bool(prowlarr["enabled"]),
            configured=bool(prowlarr["configured"]),
            status=prowlarr_status,
            message_code=(
                None
                if prowlarr["configured"] or not prowlarr["enabled"]
                else "prowlarr_not_configured"
            ),
        ),
    )
