"""Versioned Agent management and capability discovery endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    AgentCapabilitiesResponse,
    AgentTokenCreateRequest,
    AgentTokenCreateResponse,
    AgentTokenListResponse,
    AgentTokenResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.agent_tokens import AgentTokenError, AgentTokenService
from watch_assistant.services.api_errors import error_status

router = APIRouter(prefix="/api/v1/agent")


async def require_web_auth(request: Request) -> AuthContext:
    context = await require_api_auth(request)
    if context.via_bearer:
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    return context


def get_agent_token_service(request: Request) -> AgentTokenService:
    service = getattr(request.app.state, "agent_token_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="auth_not_configured")
    return service


ServiceDependency = Annotated[
    AgentTokenService, Depends(get_agent_token_service)
]
WebAuthDependency = Annotated[AuthContext, Depends(require_web_auth)]
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


@router.get("/me", response_model=AgentCapabilitiesResponse)
async def agent_me(context: AuthDependency) -> AgentCapabilitiesResponse:
    return _capabilities(context)


@router.get("/capabilities", response_model=AgentCapabilitiesResponse)
async def agent_capabilities(context: AuthDependency) -> AgentCapabilitiesResponse:
    return _capabilities(context)


@router.post("/tokens", response_model=AgentTokenCreateResponse, status_code=201)
async def create_agent_token(
    payload: AgentTokenCreateRequest,
    context: WebAuthDependency,
    service: ServiceDependency,
    request: Request,
) -> AgentTokenCreateResponse:
    try:
        return await service.create(
            payload,
            actor=context,
            request_id=getattr(request.state, "request_id", None),
        )
    except AgentTokenError as exc:
        raise _http_error(exc) from None


@router.get("/tokens", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    _: WebAuthDependency, service: ServiceDependency
) -> AgentTokenListResponse:
    return await service.list()


@router.post("/tokens/{token_id}/pause", response_model=AgentTokenResponse)
async def pause_agent_token(
    token_id: str,
    context: WebAuthDependency,
    service: ServiceDependency,
    request: Request,
) -> AgentTokenResponse:
    return await _change_state(token_id, "paused", context, service, request)


@router.post("/tokens/{token_id}/resume", response_model=AgentTokenResponse)
async def resume_agent_token(
    token_id: str,
    context: WebAuthDependency,
    service: ServiceDependency,
    request: Request,
) -> AgentTokenResponse:
    return await _change_state(token_id, "resumed", context, service, request)


@router.post("/tokens/{token_id}/revoke", response_model=AgentTokenResponse)
async def revoke_agent_token(
    token_id: str,
    context: WebAuthDependency,
    service: ServiceDependency,
    request: Request,
) -> AgentTokenResponse:
    return await _change_state(token_id, "revoked", context, service, request)


@router.post(
    "/tokens/{token_id}/rotate",
    response_model=AgentTokenCreateResponse,
    status_code=201,
)
async def rotate_agent_token(
    token_id: str,
    context: WebAuthDependency,
    service: ServiceDependency,
    request: Request,
) -> AgentTokenCreateResponse:
    try:
        return await service.rotate(
            token_id,
            actor=context,
            request_id=getattr(request.state, "request_id", None),
        )
    except AgentTokenError as exc:
        raise _http_error(exc) from None


async def _change_state(
    token_id: str,
    state: str,
    context: AuthContext,
    service: AgentTokenService,
    request: Request,
) -> AgentTokenResponse:
    try:
        return await service.change_state(
            token_id,
            state,
            actor=context,
            request_id=getattr(request.state, "request_id", None),
        )
    except AgentTokenError as exc:
        raise _http_error(exc) from None


def _capabilities(context: AuthContext) -> AgentCapabilitiesResponse:
    return AgentCapabilitiesResponse(
        token_id=context.agent_token_id,
        token_name=context.agent_name,
        scopes=sorted(context.scopes),
        library_ids=sorted(context.library_ids),
        capabilities={
            "versioned_api": True,
            "agent_tokens": True,
            "tasks": "task:read" in context.scopes,
            "organization_plans": "organize:plan" in context.scopes,
            "organization_execution": "organize:execute" in context.scopes,
            "strm": "strm:read" in context.scopes,
        },
    )


def _http_error(error: AgentTokenError) -> HTTPException:
    return HTTPException(
        status_code=error_status(error.code),
        detail={"code": error.code},
    )


__all__ = ["router"]
