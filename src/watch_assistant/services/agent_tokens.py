"""Agent Token lifecycle and the shared automation authentication contract."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import AgentToken, AuditRecord
from watch_assistant.schemas import (
    AgentTokenCreateRequest,
    AgentTokenCreateResponse,
    AgentTokenListResponse,
    AgentTokenResponse,
)
from watch_assistant.security import AGENT_SCOPES, AGENT_TOKEN_PREFIX, AuthContext
from watch_assistant.services.event_catalog import get_event_definition
from watch_assistant.services.observability import EventLogger, emit_event

DEFAULT_AGENT_TOKEN_TTL = timedelta(days=30)


class AgentTokenError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentTokenService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def create(
        self,
        request: AgentTokenCreateRequest,
        *,
        actor: AuthContext,
        request_id: str | None = None,
    ) -> AgentTokenCreateResponse:
        scopes = _validate_scopes(request.scopes)
        library_ids = _validate_library_ids(request.library_ids)
        expires_at = request.expires_at or datetime.now(UTC) + DEFAULT_AGENT_TOKEN_TTL
        expires_at = _as_utc(expires_at)
        if expires_at <= datetime.now(UTC):
            raise AgentTokenError("invalid_agent_token_request")
        raw_token = AGENT_TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        item = AgentToken(
            id="agent_" + uuid4().hex,
            name=request.name.strip(),
            token_digest=_digest(raw_token),
            token_prefix=raw_token[: len(AGENT_TOKEN_PREFIX) + 8],
            scopes_json=json.dumps(scopes, ensure_ascii=False, separators=(",", ":")),
            library_ids_json=json.dumps(
                library_ids, ensure_ascii=False, separators=(",", ":")
            ),
            expires_at=expires_at,
            created_at=now,
        )
        async with self._session_factory() as session:
            session.add(item)
            self._add_audit(
                session,
                event="agent.token.created",
                item=item,
                actor=actor,
                request_id=request_id,
                fields={"status": "active", "scopes": scopes},
            )
            await session.commit()
            response = _response(item, now=now)
        await emit_event(
            self._event_logger,
            "agent.token.created",
            fields={"status": "active"},
            request_id=request_id,
            resource_type="agent_token",
            resource_id=item.id,
        )
        return AgentTokenCreateResponse(token=raw_token, item=response)

    async def list(self) -> AgentTokenListResponse:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(select(AgentToken).order_by(AgentToken.created_at.desc()))
            )
        now = datetime.now(UTC)
        return AgentTokenListResponse(items=[_response(item, now=now) for item in items])

    async def change_state(
        self,
        token_id: str,
        state: str,
        *,
        actor: AuthContext,
        request_id: str | None = None,
    ) -> AgentTokenResponse:
        if state not in {"paused", "resumed", "revoked"}:
            raise AgentTokenError("invalid_agent_token_request")
        event = {
            "paused": "agent.token.paused",
            "resumed": "agent.token.resumed",
            "revoked": "agent.token.revoked",
        }[state]
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            item = await session.get(AgentToken, token_id)
            if item is None:
                raise AgentTokenError("agent_token_not_found")
            if item.revoked_at is not None and state != "revoked":
                raise AgentTokenError("agent_token_conflict")
            if state == "paused":
                if item.paused_at is not None:
                    raise AgentTokenError("agent_token_conflict")
                item.paused_at = now
            elif state == "resumed":
                if item.paused_at is None:
                    raise AgentTokenError("agent_token_conflict")
                item.paused_at = None
            elif item.revoked_at is None:
                item.revoked_at = now
            self._add_audit(
                session,
                event=event,
                item=item,
                actor=actor,
                request_id=request_id,
                fields={"status": state},
            )
            await session.commit()
            response = _response(item, now=now)
        await emit_event(
            self._event_logger,
            event,
            fields={"status": state},
            request_id=request_id,
            resource_type="agent_token",
            resource_id=token_id,
        )
        return response

    async def rotate(
        self,
        token_id: str,
        *,
        actor: AuthContext,
        request_id: str | None = None,
    ) -> AgentTokenCreateResponse:
        raw_token = AGENT_TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            previous = await session.get(AgentToken, token_id)
            if previous is None:
                raise AgentTokenError("agent_token_not_found")
            if previous.revoked_at is not None:
                raise AgentTokenError("agent_token_conflict")
            previous.revoked_at = now
            replacement = AgentToken(
                id="agent_" + uuid4().hex,
                name=previous.name,
                token_digest=_digest(raw_token),
                token_prefix=raw_token[: len(AGENT_TOKEN_PREFIX) + 8],
                scopes_json=previous.scopes_json,
                library_ids_json=previous.library_ids_json,
                expires_at=previous.expires_at,
                created_at=now,
            )
            session.add(replacement)
            self._add_audit(
                session,
                event="agent.token.revoked",
                item=previous,
                actor=actor,
                request_id=request_id,
                fields={"status": "rotated"},
            )
            self._add_audit(
                session,
                event="agent.token.created",
                item=replacement,
                actor=actor,
                request_id=request_id,
                fields={"status": "active"},
            )
            await session.commit()
            response = _response(replacement, now=now)
        await emit_event(
            self._event_logger,
            "agent.token.revoked",
            fields={"status": "rotated"},
            request_id=request_id,
            resource_type="agent_token",
            resource_id=token_id,
        )
        await emit_event(
            self._event_logger,
            "agent.token.created",
            fields={"status": "active"},
            request_id=request_id,
            resource_type="agent_token",
            resource_id=replacement.id,
        )
        return AgentTokenCreateResponse(token=raw_token, item=response)

    @staticmethod
    def _add_audit(
        session: AsyncSession,
        *,
        event: str,
        item: AgentToken,
        actor: AuthContext,
        request_id: str | None,
        fields: dict[str, object],
    ) -> None:
        definition = get_event_definition(event)
        if definition is None:
            raise RuntimeError(f"missing event definition: {event}")
        safe_fields = {
            key: value for key, value in fields.items() if key in definition.allowed_fields
        }
        session.add(
            AuditRecord(
                id=uuid4().hex,
                timestamp=datetime.now(UTC),
                event_code=definition.code,
                event_version=definition.version,
                title_zh=definition.title_zh,
                message_zh=definition.render(safe_fields),
                suggestion_zh=definition.suggestion_zh,
                status=str(safe_fields.get("status", "completed")),
                request_id=request_id,
                actor_type="session" if not actor.via_bearer else "agent",
                actor_id=actor.agent_token_id or actor.identity,
                resource_type="agent_token",
                resource_id=item.id,
                context_json=json.dumps(
                    {"scopes": _decode(item.scopes_json)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )


def _validate_scopes(values: list[str]) -> list[str]:
    if not values:
        values = ["system:read", "library:read", "task:read"]
    normalized = sorted({value.strip() for value in values if value.strip()})
    if not normalized or any(value not in AGENT_SCOPES for value in normalized):
        raise AgentTokenError("invalid_agent_token_request")
    return normalized


def _validate_library_ids(values: list[str]) -> list[str]:
    normalized = sorted({value.strip() for value in values if value.strip()})
    if any(len(value) > 128 or any(char in value for char in "\\/") for value in normalized):
        raise AgentTokenError("invalid_agent_token_request")
    return normalized


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decode(value: str) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in decoded if isinstance(item, str)] if isinstance(decoded, list) else []


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _response(item: AgentToken, *, now: datetime) -> AgentTokenResponse:
    expires_at = _as_utc(item.expires_at) if item.expires_at is not None else None
    if item.revoked_at is not None:
        status = "revoked"
    elif item.paused_at is not None:
        status = "paused"
    elif expires_at is not None and expires_at <= now:
        status = "expired"
    else:
        status = "active"
    return AgentTokenResponse(
        id=item.id,
        name=item.name,
        token_prefix=item.token_prefix,
        scopes=_decode(item.scopes_json),
        library_ids=_decode(item.library_ids_json),
        expires_at=expires_at,
        paused_at=_as_utc(item.paused_at) if item.paused_at is not None else None,
        revoked_at=_as_utc(item.revoked_at) if item.revoked_at is not None else None,
        created_at=_as_utc(item.created_at),
        last_used_at=_as_utc(item.last_used_at) if item.last_used_at is not None else None,
        last_client_version=item.last_client_version,
        call_count=item.call_count,
        status=status,
    )


__all__ = ["AgentTokenError", "AgentTokenService"]
