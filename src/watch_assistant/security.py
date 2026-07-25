"""Authentication, CSRF, rate limiting, and log redaction primitives."""

import hashlib
import inspect
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import WebSession

SESSION_COOKIE = "watch_session"
SESSION_TTL = timedelta(hours=12)


class AuthError(HTTPException):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)


@dataclass
class SessionRecord:
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthContext:
    identity: str
    via_bearer: bool
    session_id: str | None = None
    csrf_token: str | None = None


class SecurityManager:
    def __init__(
        self,
        *,
        web_password_hash: str,
        script_token_hash: str,
        cookie_secure: bool = False,
        push_limit: int = 10,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        session_ttl: timedelta = SESSION_TTL,
    ) -> None:
        self._password_hash = PasswordHash.recommended()
        self._web_password_hash = web_password_hash
        self._script_token_hash = script_token_hash
        self.cookie_secure = cookie_secure
        self.push_limit = push_limit
        self._session_factory = session_factory
        self._session_ttl = session_ttl
        self._credential_fingerprint = hashlib.sha256(
            web_password_hash.encode("utf-8")
        ).hexdigest()
        self._sessions: dict[str, SessionRecord] = {}
        self._legacy_sessions: dict[str, SessionRecord] = {}
        self._rate_windows: dict[tuple[str, str], deque[datetime]] = {}

    @property
    def session_ttl_seconds(self) -> int:
        return int(self._session_ttl.total_seconds())

    def configure_session_store(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session_ttl: timedelta | None = None,
    ) -> None:
        if self._session_factory is None:
            self._legacy_sessions.update(self._sessions)
            self._sessions.clear()
        self._session_factory = session_factory
        if session_ttl is not None:
            self._session_ttl = session_ttl

    def login(self, password: str) -> tuple[str, str]:
        try:
            valid = self._password_hash.verify(password, self._web_password_hash)
        except Exception:  # noqa: BLE001 - invalid configured hash fails closed
            valid = False
        if not valid:
            raise AuthError(401, "invalid_credentials")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        target = self._legacy_sessions if self._session_factory else self._sessions
        target[session_id] = SessionRecord(
            csrf_token=csrf_token,
            expires_at=datetime.now(UTC) + SESSION_TTL,
        )
        return session_id, csrf_token

    async def login_async(self, password: str) -> tuple[str, str]:
        self._verify_web_password(password)
        if self._session_factory is None:
            return self.login(password)
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    delete(WebSession).where(WebSession.expires_at <= now)
                )
                session.add(
                    WebSession(
                        session_digest=self._session_digest(session_id),
                        csrf_token=csrf_token,
                        credential_fingerprint=self._credential_fingerprint,
                        created_at=now,
                        expires_at=now + self._session_ttl,
                    )
                )
                await session.commit()
        except Exception as exc:
            raise AuthError(503, "auth_unavailable") from exc
        return session_id, csrf_token

    def logout(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    async def logout_async(self, session_id: str | None) -> None:
        if not session_id:
            return
        if self._session_factory is None:
            self.logout(session_id)
            return
        try:
            async with self._session_factory() as session:
                await session.execute(
                    delete(WebSession).where(
                        WebSession.session_digest == self._session_digest(session_id)
                    )
                )
                await session.commit()
        except Exception as exc:
            raise AuthError(503, "auth_unavailable") from exc
        self._sessions.pop(session_id, None)
        self._legacy_sessions.pop(session_id, None)

    def authenticate(self, request: Request) -> AuthContext:
        return self._authenticate_memory(request)

    async def authenticate_async(self, request: Request) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        if authorization:
            context = self._bearer_context(authorization)
        elif self._session_factory is None:
            return self._authenticate_memory(request)
        else:
            session_id = request.cookies.get(SESSION_COOKIE)
            if not session_id:
                raise AuthError(401, "unauthorized")
            context = await self._database_context(session_id)

        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not context.via_bearer
            and request.headers.get("X-CSRF-Token") != context.csrf_token
        ):
            raise AuthError(403, "csrf_required")
        self._check_rate_limit(request, context.identity)
        return context

    def _authenticate_memory(self, request: Request) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        if authorization:
            context = self._bearer_context(authorization)
        else:
            session_id = request.cookies.get(SESSION_COOKIE)
            record = self._sessions.get(session_id or "")
            if record is None or record.expires_at <= datetime.now(UTC):
                if session_id:
                    self._sessions.pop(session_id, None)
                raise AuthError(401, "unauthorized")
            context = AuthContext(
                identity="session:" + hashlib.sha256(session_id.encode()).hexdigest(),
                via_bearer=False,
                session_id=session_id,
                csrf_token=record.csrf_token,
            )
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not context.via_bearer
            and request.headers.get("X-CSRF-Token") != context.csrf_token
        ):
            raise AuthError(403, "csrf_required")
        self._check_rate_limit(request, context.identity)
        return context

    def _bearer_context(self, authorization: str) -> AuthContext:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise AuthError(401, "unauthorized")
        try:
            valid = self._password_hash.verify(token, self._script_token_hash)
        except Exception:  # noqa: BLE001 - invalid configured hash fails closed
            valid = False
        if not valid:
            raise AuthError(401, "unauthorized")
        return AuthContext(
            identity="bearer:" + hashlib.sha256(token.encode()).hexdigest(),
            via_bearer=True,
        )

    async def _database_context(self, session_id: str) -> AuthContext:
        digest = self._session_digest(session_id)
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                record = await session.get(WebSession, digest)
                expires_at = _as_utc(record.expires_at) if record else None
                if record is None:
                    legacy_record = self._legacy_sessions.get(session_id)
                    if legacy_record is not None:
                        if legacy_record.expires_at > now:
                            return AuthContext(
                                identity="session:" + digest,
                                via_bearer=False,
                                session_id=session_id,
                                csrf_token=legacy_record.csrf_token,
                            )
                        self._legacy_sessions.pop(session_id, None)
                if (
                    record is None
                    or expires_at <= now
                    or record.credential_fingerprint != self._credential_fingerprint
                ):
                    if record is not None:
                        await session.delete(record)
                        await session.commit()
                    raise AuthError(401, "unauthorized")
                return AuthContext(
                    identity="session:" + digest,
                    via_bearer=False,
                    session_id=session_id,
                    csrf_token=record.csrf_token,
                )
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(401, "unauthorized") from exc

    def _verify_web_password(self, password: str) -> None:
        try:
            valid = self._password_hash.verify(password, self._web_password_hash)
        except Exception:  # noqa: BLE001 - invalid configured hash fails closed
            valid = False
        if not valid:
            raise AuthError(401, "invalid_credentials")

    @staticmethod
    def _session_digest(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _check_rate_limit(self, request: Request, identity: str) -> None:
        path = request.url.path
        if request.method != "POST":
            return
        if path == "/api/v1/search":
            bucket, limit = "search", 30
        elif path == "/api/v1/cache/retry":
            bucket, limit = "maintenance", 2
        elif path.startswith("/api/v1/tasks"):
            bucket, limit = "push", self.push_limit
        else:
            return
        now = datetime.now(UTC)
        window = self._rate_windows.setdefault((identity, bucket), deque())
        cutoff = now - timedelta(minutes=1)
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limit:
            raise AuthError(429, "rate_limited")
        window.append(now)


async def require_api_auth(request: Request) -> AuthContext:
    manager: SecurityManager | None = getattr(
        request.app.state, "security_manager", None
    )
    if manager is None:
        return AuthContext(identity="internal", via_bearer=True)
    result = manager.authenticate_async(request)
    if inspect.isawaitable(result):
        return await result
    return result


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def redact_mapping(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    sensitive_keys = ("url", "password", "token", "authorization", "cookie", "api_key")
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(part in str(key).casefold() for part in sensitive_keys)
            else redact_mapping(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value
