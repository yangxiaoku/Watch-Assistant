"""Authenticated HTTP transport for the allow-listed MCP adapter."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.mcp import McpService

router = APIRouter(prefix="/api/v1/mcp")
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def get_service(request: Request) -> McpService:
    return request.app.state.mcp_service


@router.post("")
async def mcp_json_rpc(
    payload: dict[str, Any], context: AuthDependency, request: Request
) -> dict[str, Any]:
    service = get_service(request)
    return await service.handle(
        payload,
        context=context,
        request_id=getattr(request.state, "request_id", None),
        correlation_id=getattr(request.state, "correlation_id", None),
    )


__all__ = ["router"]
