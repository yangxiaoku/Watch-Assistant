"""Authenticated controls for online maintenance mode."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import MaintenanceEnterRequest, MaintenanceStatusResponse
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.maintenance_gate import MaintenanceGate

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_gate(request: Request) -> MaintenanceGate:
    gate = getattr(request.app.state, "maintenance_gate", None)
    if gate is None:
        raise HTTPException(status_code=503, detail="maintenance_unavailable")
    return gate


GateDependency = Annotated[MaintenanceGate, Depends(get_gate)]


@router.get("/maintenance/status", response_model=MaintenanceStatusResponse)
async def maintenance_status(gate: GateDependency) -> MaintenanceStatusResponse:
    return await gate.status()


@router.post("/maintenance/enter", response_model=MaintenanceStatusResponse)
async def enter_maintenance(
    payload: MaintenanceEnterRequest,
    gate: GateDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> MaintenanceStatusResponse:
    return await gate.enter(reason=payload.reason, actor_id=auth.identity)


@router.post("/maintenance/release", response_model=MaintenanceStatusResponse)
async def release_maintenance(
    gate: GateDependency,
    auth: Annotated[AuthContext, Depends(require_api_auth)],
) -> MaintenanceStatusResponse:
    del auth
    return await gate.leave()
