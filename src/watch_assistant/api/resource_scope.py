"""Shared media-library scope checks for bearer API resources."""

from fastapi import HTTPException, Request
from sqlalchemy import select

from watch_assistant.library_models import OrganizationPlan
from watch_assistant.models import OrganizationOperation
from watch_assistant.security import AuthContext


def scoped_library_ids(context: AuthContext) -> frozenset[str] | None:
    """Return an Agent's explicit library scope, or None for unscoped callers."""

    if context.via_bearer and context.library_ids:
        return context.library_ids
    return None


async def require_plan_library_scope(
    request: Request, context: AuthContext, plan_id: str
) -> None:
    """Hide plans outside an Agent's allowed libraries as not found."""

    allowed = scoped_library_ids(context)
    if allowed is None:
        return
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="organization_plan_unavailable")
    async with database.session_factory() as session:
        library_id = await session.scalar(
            select(OrganizationPlan.library_id).where(OrganizationPlan.id == plan_id)
        )
    if library_id not in allowed:
        raise _not_found("plan_not_found", "计划不存在")


async def require_operation_library_scope(
    request: Request, context: AuthContext, operation_id: str
) -> None:
    """Hide operations whose plan belongs to another allowed-library scope."""

    allowed = scoped_library_ids(context)
    if allowed is None:
        return
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="organization_operation_unavailable")
    async with database.session_factory() as session:
        library_id = await session.scalar(
            select(OrganizationPlan.library_id)
            .join(
                OrganizationOperation,
                OrganizationOperation.plan_id == OrganizationPlan.id,
            )
            .where(OrganizationOperation.id == operation_id)
        )
    if library_id not in allowed:
        raise _not_found("operation_not_found", "整理操作不存在")


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


__all__ = [
    "require_operation_library_scope",
    "require_plan_library_scope",
    "scoped_library_ids",
]
