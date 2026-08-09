"""Web session authentication routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from watch_assistant.schemas import AuthLoginRequest, AuthLoginResponse
from watch_assistant.security import (
    SESSION_COOKIE,
    AuthContext,
    SecurityManager,
    require_api_auth,
)

router = APIRouter(prefix="/api/v1/auth")
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def get_security_manager(request: Request) -> SecurityManager:
    manager = getattr(request.app.state, "security_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="auth_not_configured")
    return manager


SecurityManagerDependency = Annotated[SecurityManager, Depends(get_security_manager)]


@router.post("/login", response_model=AuthLoginResponse)
async def login(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
    manager: SecurityManagerDependency,
) -> AuthLoginResponse:
    manager.check_login_rate_limit(request)
    session_id, csrf_token = await manager.login_async(
        payload.password, username=payload.username
    )
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=manager.cookie_secure,
        samesite="lax",
        max_age=manager.session_ttl_seconds,
        path="/",
    )
    return AuthLoginResponse(csrf_token=csrf_token)


@router.post("/logout")
async def logout(
    response: Response,
    context: AuthDependency,
    manager: SecurityManagerDependency,
) -> dict[str, str]:
    await manager.logout_async(context.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me")
async def me(context: AuthDependency) -> dict[str, str | bool | None]:
    return {
        "authenticated": True,
        "via_bearer": context.via_bearer,
        "csrf_token": context.csrf_token,
    }
