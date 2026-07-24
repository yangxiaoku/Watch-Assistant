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


SecurityManagerDependency = Annotated[
    SecurityManager, Depends(get_security_manager)
]


@router.post("/login", response_model=AuthLoginResponse)
async def login(
    payload: AuthLoginRequest,
    response: Response,
    manager: SecurityManagerDependency,
) -> AuthLoginResponse:
    session_id, csrf_token = manager.login(payload.password)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=manager.cookie_secure,
        samesite="lax",
        max_age=43200,
        path="/",
    )
    return AuthLoginResponse(csrf_token=csrf_token)


@router.post("/logout")
async def logout(
    response: Response,
    context: AuthDependency,
    manager: SecurityManagerDependency,
) -> dict[str, str]:
    manager.logout(context.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me")
async def me(context: AuthDependency) -> dict[str, str | bool]:
    return {"authenticated": True, "via_bearer": context.via_bearer}
