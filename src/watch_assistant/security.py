"""Authentication, CSRF, rate limiting, and log redaction primitives."""

import hashlib
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

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
    ) -> None:
        self._password_hash = PasswordHash.recommended()
        self._web_password_hash = web_password_hash
        self._script_token_hash = script_token_hash
        self.cookie_secure = cookie_secure
        self.push_limit = push_limit
        self._sessions: dict[str, SessionRecord] = {}
        self._rate_windows: dict[tuple[str, str], deque[datetime]] = {}

    def login(self, password: str) -> tuple[str, str]:
        try:
            valid = self._password_hash.verify(password, self._web_password_hash)
        except Exception:  # noqa: BLE001 - invalid configured hash fails closed
            valid = False
        if not valid:
            raise AuthError(401, "invalid_credentials")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        self._sessions[session_id] = SessionRecord(
            csrf_token=csrf_token,
            expires_at=datetime.now(UTC) + SESSION_TTL,
        )
        return session_id, csrf_token

    def logout(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def authenticate(self, request: Request) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not token:
                raise AuthError(401, "unauthorized")
            try:
                valid = self._password_hash.verify(token, self._script_token_hash)
            except Exception:  # noqa: BLE001 - invalid configured hash fails closed
                valid = False
            if not valid:
                raise AuthError(401, "unauthorized")
            context = AuthContext(
                identity="bearer:" + hashlib.sha256(token.encode()).hexdigest(),
                via_bearer=True,
            )
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

    def _check_rate_limit(self, request: Request, identity: str) -> None:
        path = request.url.path
        if request.method != "POST":
            return
        if path == "/api/v1/search":
            bucket, limit = "search", 30
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
    return manager.authenticate(request)


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
