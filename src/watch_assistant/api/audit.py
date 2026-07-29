"""Authenticated, bounded audit record queries."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from watch_assistant.models import AuditRecord
from watch_assistant.schemas import AuditRecordListResponse, AuditRecordResponse
from watch_assistant.security import require_api_auth

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


@router.get("/audit", response_model=AuditRecordListResponse)
async def list_audit(
    request: Request,
    event_code: Annotated[str | None, Query(max_length=128)] = None,
    actor: Annotated[str | None, Query(max_length=128)] = None,
    since: datetime | None = None,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditRecordListResponse:
    query = select(AuditRecord).order_by(AuditRecord.timestamp.desc(), AuditRecord.id.desc())
    if event_code:
        query = query.where(AuditRecord.event_code == event_code)
    if actor:
        query = query.where(AuditRecord.actor_id == actor)
    if since is not None:
        query = query.where(AuditRecord.timestamp >= since)
    async with request.app.state.database.session_factory() as session:
        rows = list((await session.scalars(query.offset(cursor).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return AuditRecordListResponse(
        items=[_response(row) for row in rows],
        next_cursor=cursor + limit if has_more else None,
    )


@router.get("/audit/{audit_id}", response_model=AuditRecordResponse)
async def get_audit(audit_id: str, request: Request) -> AuditRecordResponse:
    async with request.app.state.database.session_factory() as session:
        row = await session.get(AuditRecord, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="audit_not_found")
    return _response(row)


def _response(row: AuditRecord) -> AuditRecordResponse:
    return AuditRecordResponse(
        audit_id=row.id,
        timestamp=row.timestamp,
        event_code=row.event_code,
        event_version=row.event_version,
        title_zh=row.title_zh,
        message_zh=row.message_zh,
        suggestion_zh=row.suggestion_zh,
        status=row.status,
        request_id=row.request_id,
        correlation_id=row.correlation_id,
        actor_type=row.actor_type,
        resource_type=row.resource_type,
        task_id=row.task_id,
    )


__all__ = ["router"]
