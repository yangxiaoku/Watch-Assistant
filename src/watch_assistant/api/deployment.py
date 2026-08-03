"""Authenticated, read-only deployment diagnostics routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import DeploymentDiagnosticsResponse
from watch_assistant.security import AuthContext, require_diagnostics_auth
from watch_assistant.services.deployment_diagnostics import DeploymentDiagnosticsService

router = APIRouter(prefix="/api/v1")


def get_deployment_service(request: Request) -> DeploymentDiagnosticsService:
    service = getattr(request.app.state, "deployment_diagnostics_service", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="deployment_diagnostics_unavailable"
        )
    return service


DeploymentDependency = Annotated[
    DeploymentDiagnosticsService, Depends(get_deployment_service)
]
DiagnosticsAuthDependency = Annotated[AuthContext, Depends(require_diagnostics_auth)]


@router.get("/deployment/diagnostics", response_model=DeploymentDiagnosticsResponse)
async def deployment_diagnostics(
    _: DiagnosticsAuthDependency,
    service: DeploymentDependency,
) -> DeploymentDiagnosticsResponse:
    return await service.snapshot()
