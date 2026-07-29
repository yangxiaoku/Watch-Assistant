"""Authenticated quality profile and simulation routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    QualityProfileCreateRequest,
    QualityProfilePatch,
    QualityProfileResponse,
    QualitySimulationRequest,
    QualitySimulationResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.quality_profiles import (
    QualityProfileConflict,
    QualityProfileNotFound,
    QualityProfileService,
    QualityProfileValidationError,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_quality_service(request: Request) -> QualityProfileService:
    service = getattr(request.app.state, "quality_profile_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="quality_profiles_unavailable")
    return service


QualityServiceDependency = Annotated[
    QualityProfileService, Depends(get_quality_service)
]


@router.post("/quality-profiles", response_model=QualityProfileResponse, status_code=201)
async def create_quality_profile(
    payload: QualityProfileCreateRequest,
    service: QualityServiceDependency,
) -> QualityProfileResponse:
    try:
        return await service.create(payload)
    except QualityProfileValidationError as exc:
        code = str(exc)
        if code not in {"invalid_quality_rules", "unknown_quality_rule", "invalid_min_resolution"}:
            code = "invalid_quality_rules"
        raise HTTPException(status_code=422, detail=code) from None


@router.get("/quality-profiles", response_model=list[QualityProfileResponse])
async def list_quality_profiles(
    service: QualityServiceDependency,
) -> list[QualityProfileResponse]:
    return await service.list()


@router.get("/quality-profiles/{profile_id}", response_model=QualityProfileResponse)
async def get_quality_profile(
    profile_id: str,
    service: QualityServiceDependency,
) -> QualityProfileResponse:
    try:
        return await service.get(profile_id)
    except QualityProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="quality_profile_not_found") from exc


@router.patch("/quality-profiles/{profile_id}", response_model=QualityProfileResponse)
async def update_quality_profile(
    profile_id: str,
    payload: QualityProfilePatch,
    service: QualityServiceDependency,
) -> QualityProfileResponse:
    try:
        return await service.update(profile_id, payload)
    except QualityProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="quality_profile_not_found") from exc
    except QualityProfileConflict:
        raise HTTPException(status_code=409, detail="quality_profile_conflict") from None
    except QualityProfileValidationError as exc:
        code = str(exc)
        if code not in {"invalid_quality_rules", "unknown_quality_rule", "invalid_min_resolution"}:
            code = "invalid_quality_rules"
        raise HTTPException(status_code=422, detail=code) from None


@router.post(
    "/quality-profiles/{profile_id}/simulate",
    response_model=QualitySimulationResponse,
)
async def simulate_quality_profile(
    profile_id: str,
    payload: QualitySimulationRequest,
    service: QualityServiceDependency,
) -> QualitySimulationResponse:
    try:
        return await service.simulate(profile_id, payload.resource_ids)
    except QualityProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="quality_profile_not_found") from exc
