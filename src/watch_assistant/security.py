"""Authentication, CSRF, rate limiting, and log redaction primitives."""

import hashlib
import inspect
import json
import os
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import AgentToken, WebSession

SESSION_COOKIE = "watch_session"
SESSION_TTL = timedelta(hours=12)
DEFAULT_ADMIN_USERNAME = "admin"
AGENT_TOKEN_PREFIX = "wa_at_"
AGENT_SCOPES = frozenset(
    {
        "system:read",
        "library:read",
        "library:write",
        "task:read",
        "task:write",
        "organize:plan",
        "organize:execute",
        "review:write",
        "strm:read",
        "strm:write",
        "cleanup:plan",
        "cleanup:execute",
        "settings:read",
        "settings:write",
        "audit:read",
        "backup:read",
        "backup:write",
    }
)


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
    token_kind: str = "session"
    agent_token_id: str | None = None
    agent_name: str | None = None
    scopes: frozenset[str] = frozenset()
    library_ids: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        return not self.via_bearer or scope in self.scopes


class SecurityManager:
    def __init__(
        self,
        *,
        web_password_hash: str | None,
        script_token_hash: str,
        web_username: str = DEFAULT_ADMIN_USERNAME,
        bootstrap_admin_enabled: bool = False,
        bootstrap_admin_password: str | None = None,
        diagnostics_token: str = "",
        cookie_secure: bool = False,
        push_limit: int = 10,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        session_ttl: timedelta = SESSION_TTL,
        event_logger: Any | None = None,
    ) -> None:
        self._password_hash = PasswordHash.recommended()
        self._web_password_hash = web_password_hash
        self._script_token_hash = script_token_hash
        self._web_username = web_username
        self._bootstrap_admin_enabled = bootstrap_admin_enabled
        self._bootstrap_admin_password = bootstrap_admin_password
        if not web_username:
            raise ValueError("web_username must not be empty")
        if bootstrap_admin_enabled and web_username != DEFAULT_ADMIN_USERNAME:
            raise ValueError(
                "bootstrap_admin_enabled requires the admin username"
            )
        if bootstrap_admin_enabled and web_password_hash:
            raise ValueError(
                "web_password_hash must be absent when admin bootstrap is enabled"
            )
        if not web_password_hash and not bootstrap_admin_enabled:
            raise ValueError(
                "web_password_hash is required unless admin bootstrap is enabled"
            )
        if bootstrap_admin_enabled and not bootstrap_admin_password:
            raise ValueError(
                "bootstrap_admin_password is required when admin bootstrap is enabled"
            )
        self._diagnostics_token = diagnostics_token
        self.cookie_secure = cookie_secure
        self.push_limit = push_limit
        self._session_factory = session_factory
        self._session_ttl = session_ttl
        self._event_logger = event_logger
        fingerprint_input = web_password_hash or ""
        bootstrap_fingerprint = (
            hashlib.sha256(bootstrap_admin_password.encode("utf-8")).hexdigest()
            if bootstrap_admin_password
            else ""
        )
        if (
            web_username != DEFAULT_ADMIN_USERNAME
            or bootstrap_admin_enabled
            or bootstrap_fingerprint
        ):
            fingerprint_input = "\0".join(
                (
                    web_username,
                    fingerprint_input,
                    "bootstrap" if bootstrap_admin_enabled else "configured",
                    bootstrap_fingerprint,
                )
            )
        self._credential_fingerprint = hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest()
        self._sessions: dict[str, SessionRecord] = {}
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
        self._session_factory = session_factory
        self._sessions.clear()
        if session_ttl is not None:
            self._session_ttl = session_ttl

    def configure_event_logger(self, event_logger: Any | None) -> None:
        self._event_logger = event_logger

    def login(
        self, password: str, *, username: str | None = None
    ) -> tuple[str, str]:
        if self._session_factory is not None:
            raise AuthError(503, "auth_unavailable")
        self._verify_web_credentials(username, password)
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        self._sessions[session_id] = SessionRecord(
            csrf_token=csrf_token,
            expires_at=datetime.now(UTC) + self._session_ttl,
        )
        return session_id, csrf_token

    async def login_async(
        self, password: str, *, username: str | None = None
    ) -> tuple[str, str]:
        self._verify_web_credentials(username, password)
        if self._session_factory is None:
            return self.login(password, username=username)
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
        except Exception:  # noqa: BLE001 - storage failures fail closed
            raise AuthError(503, "auth_unavailable") from None
        return session_id, csrf_token

    def logout(self, session_id: str | None) -> None:
        if self._session_factory is not None:
            raise AuthError(503, "auth_unavailable")
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
        except Exception:  # noqa: BLE001 - storage failures fail closed
            raise AuthError(503, "auth_unavailable") from None
        self._sessions.pop(session_id, None)

    def authenticate(self, request: Request) -> AuthContext:
        if self._session_factory is not None:
            raise AuthError(503, "auth_unavailable")
        return self._authenticate_memory(request)

    async def authenticate_async(self, request: Request) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        if authorization:
            if self._session_factory is None:
                context = self._bearer_context(authorization)
            else:
                context = await self._bearer_context_async(authorization, request)
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

    async def authenticate_diagnostics_async(self, request: Request) -> AuthContext:
        """Authenticate the read-only deployment diagnostics endpoint."""
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if (
            scheme.casefold() == "bearer"
            and token
            and self._diagnostics_token
            and secrets.compare_digest(token, self._diagnostics_token)
        ):
            context = AuthContext(
                identity="diagnostics:"
                + hashlib.sha256(token.encode("utf-8")).hexdigest(),
                via_bearer=True,
                token_kind="diagnostics",
                scopes=frozenset({"system:read"}),
            )
            self._check_rate_limit(request, context.identity)
            return context
        return await self.authenticate_async(request)

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
            token_kind="legacy",
            scopes=frozenset(AGENT_SCOPES),
        )

    async def _bearer_context_async(
        self, authorization: str, request: Request
    ) -> AuthContext:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise AuthError(401, "unauthorized")
        try:
            valid = self._password_hash.verify(token, self._script_token_hash)
        except Exception:  # noqa: BLE001 - invalid configured hash fails closed
            valid = False
        if valid:
            return AuthContext(
                identity="bearer:" + hashlib.sha256(token.encode()).hexdigest(),
                via_bearer=True,
                token_kind="legacy",
                scopes=frozenset(AGENT_SCOPES),
            )
        return await self._agent_token_context(token, request)

    async def _agent_token_context(
        self, token: str, request: Request
    ) -> AuthContext:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(AgentToken).where(AgentToken.token_digest == digest)
                )
                if record is None:
                    raise AuthError(401, "unauthorized")
                expires_at = (
                    _as_utc(record.expires_at) if record.expires_at is not None else None
                )
                if (
                    record.revoked_at is not None
                    or record.paused_at is not None
                    or (expires_at is not None and expires_at <= now)
                ):
                    raise AuthError(401, "unauthorized")
                record.last_used_at = now
                record.last_used_ip = _client_host(request)
                record.last_client_version = _safe_client_version(
                    request.headers.get("X-Watch-Assistant-Client-Version")
                )
                record.call_count += 1
                await session.commit()
                context = AuthContext(
                    identity="agent:" + record.id,
                    via_bearer=True,
                    token_kind="agent",
                    agent_token_id=record.id,
                    agent_name=record.name,
                    scopes=frozenset(_decode_json_strings(record.scopes_json)),
                    library_ids=frozenset(_decode_json_strings(record.library_ids_json)),
                )
        except AuthError:
            raise
        except Exception:  # noqa: BLE001 - storage failures fail closed
            raise AuthError(503, "auth_unavailable") from None
        await _emit_security_event(
            self._event_logger,
            "agent.request.authenticated",
            request=request,
            actor_id=context.agent_token_id,
        )
        return context

    async def _database_context(self, session_id: str) -> AuthContext:
        digest = self._session_digest(session_id)
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                record = await session.get(WebSession, digest)
                expires_at = _as_utc(record.expires_at) if record else None
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
        except Exception:  # noqa: BLE001 - storage failures fail closed
            raise AuthError(503, "auth_unavailable") from None

    def _verify_web_credentials(self, username: str | None, password: str) -> None:
        effective_username = self._web_username if username is None else username
        username_valid = secrets.compare_digest(
            effective_username, self._web_username
        )
        password_valid = False
        if self._web_password_hash:
            try:
                password_valid = self._password_hash.verify(
                    password, self._web_password_hash
                )
            except Exception:  # noqa: BLE001 - invalid configured hash fails closed
                password_valid = False
        bootstrap_valid = (
            self._bootstrap_admin_enabled
            and self._bootstrap_admin_password is not None
            and secrets.compare_digest(effective_username, DEFAULT_ADMIN_USERNAME)
            and secrets.compare_digest(password, self._bootstrap_admin_password)
        )
        if not username_valid or not (password_valid or bootstrap_valid):
            raise AuthError(401, "invalid_credentials")

    def _verify_web_password(self, password: str) -> None:
        """Keep the password-only helper compatible with internal callers."""

        self._verify_web_credentials(None, password)

    @staticmethod
    def _session_digest(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def check_login_rate_limit(self, request: Request) -> None:
        """Rate-limit web login attempts by client address.

        Login runs before any authenticated dependency, so the shared
        post-auth rate limiter never sees it without this explicit entry.
        """
        host = _client_host(request)
        identity = f"login:{host}" if host is not None else "login:unknown"
        self._check_rate_limit(request, identity)

    def _check_rate_limit(self, request: Request, identity: str) -> None:
        path = request.url.path
        if request.method != "POST":
            if request.method == "PATCH" and path == "/api/v1/settings/inspection":
                bucket, limit = "inspection_settings", 10
            else:
                return
        elif path == "/api/v1/auth/login":
            bucket, limit = "login", 5
        elif path == "/api/v1/search":
            bucket, limit = "search", 30
        elif path == "/api/v1/cache/retry":
            bucket, limit = "maintenance", 2
        elif path == "/api/v1/resources/inspect":
            bucket, limit = "inspection", 10
        elif path == "/api/v1/mcp":
            bucket, limit = "mcp", 60
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
        # Fail closed: a missing security manager must reject every request,
        # never open access (the lite create_app path is test-only).
        raise AuthError(503, "auth_unavailable")
    return await _require_authenticated_context(
        request, manager, manager.authenticate_async
    )


async def require_diagnostics_auth(request: Request) -> AuthContext:
    manager: SecurityManager | None = getattr(
        request.app.state, "security_manager", None
    )
    if manager is None:
        raise AuthError(503, "auth_unavailable")
    return await _require_authenticated_context(
        request, manager, manager.authenticate_diagnostics_async
    )


async def _require_authenticated_context(
    request: Request, manager: SecurityManager, authenticate: Any
) -> AuthContext:
    result = authenticate(request)
    if inspect.isawaitable(result):
        context = await result
    else:
        context = result
    required_scope = _required_scope(request)
    if context.token_kind == "agent" and required_scope not in context.scopes:
        await _emit_security_event(
            getattr(manager, "_event_logger", None),
            "agent.permission_denied",
            request=request,
            actor_id=context.agent_token_id,
        )
        raise AuthError(
            403,
            {"code": "missing_scope", "missing_scopes": [required_scope]},
        )
    return context


def require_scope(scope: str):
    """Build a dependency for adapters that expose a narrower route contract."""

    if scope not in AGENT_SCOPES:
        raise ValueError(f"unknown agent scope: {scope}")

    async def dependency(
        context: AuthContext = Depends(require_api_auth),  # noqa: B008
    ) -> AuthContext:
        if not context.has_scope(scope):
            raise AuthError(
                403,
                {"code": "missing_scope", "missing_scopes": [scope]},
            )
        return context

    return dependency


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


def _required_scope(request: Request) -> str:
    path = request.url.path
    method = request.method.upper()
    if path in {"/api/v1/health", "/api/v1/mcp"} or path.startswith("/api/v1/agent/"):
        return "system:read"
    if path.startswith("/api/v1/auth/"):
        # 登录/登出/会话查询走 web session(via_bearer=False 时 scope 不校验);
        # agent token 调用 auth 路由要求最小系统读权限即可。
        return "system:read"
    if path.startswith("/api/v1/deployment/"):
        # 部署诊断走专用 diagnostics token(require_diagnostics_auth),此处仅
        # 确保不走 fail-closed 兜底;agent token 无专用 scope 时仍需最小读权限。
        return "system:read"
    if path.startswith("/api/v1/tasks"):
        return "task:read" if method in {"GET", "HEAD"} else "task:write"
    if path.startswith("/api/v1/workflows"):
        # workflow 审批/取消/发现/阶段推进都是组织写操作,不得与任务写共用
        # task:write,否则低权限 token 可推进 workflow 终态。
        return "task:read" if method in {"GET", "HEAD"} else "organize:execute"
    if path.startswith("/api/v1/notifications"):
        return "task:read" if method in {"GET", "HEAD"} else "task:write"
    if path.startswith("/api/v1/organization-operations"):
        return "task:read" if method in {"GET", "HEAD"} else "organize:execute"
    if path.startswith("/api/v1/organization-plans/") and path.endswith(
        ("/operation", "/confirm-and-operation")
    ):
        return "organize:execute"
    if path.startswith("/api/v1/organization-plans"):
        return "organize:plan"
    if path.startswith("/api/v1/organization-history"):
        # 组织历史含目标路径等敏感详情,读权限收敛到 organize:plan,
        # 不得落回默认 system:read。
        return "organize:plan"
    if path.startswith("/api/v1/imports"):
        # 手动导入是组织写操作,不得落回默认 task:write。
        return "organize:execute"
    if path.startswith("/api/v1/strm/play"):
        return "strm:read"
    if path.startswith(("/api/v1/strm-operations", "/api/v1/strm-cleanup-plans")):
        return "strm:read" if method in {"GET", "HEAD"} else "strm:write"
    if path.startswith("/api/v1/empty-directory-cleanup-plans"):
        return "library:read" if method in {"GET", "HEAD"} else "organize:execute"
    if path.startswith("/api/v1/libraries/") and any(
        marker in path
        for marker in (
            "/strm-generation",
            "/strm-incremental",
            "/strm-cleanup",
            "/strm-cleanup-plan",
            "/strm-manifest",
            "/strm-operations",
        )
    ):
        return "strm:write" if method not in {"GET", "HEAD"} else "strm:read"
    if path.startswith("/api/v1/libraries/") and "/objects/" in path and path.endswith("/delete"):
        return "organize:execute"
    if path.startswith(("/api/v1/settings", "/api/v1/notification-preferences")):
        return "settings:read" if method in {"GET", "HEAD"} else "settings:write"
    if path.startswith(("/api/v1/search", "/api/v1/movie")):
        return "library:read"
    if (
        path.startswith("/api/v1/libraries/")
        and path.endswith("/configuration")
        and method not in {"GET", "HEAD"}
    ):
        # 库配置(声明生产根)是设置管理,路由声明 require_scope("settings:write"),
        # 全局映射必须与之对齐,否则 bearer token 需同时满足两个 scope。
        return "settings:write"
    if path.startswith(("/api/v1/libraries", "/api/v1/media")):
        # 写方法(扫描/取消/计划/配置/身份绑定)必须要求 library:write,
        # 不得与只读共用 library:read,否则只读 agent token 可触发全库扫描。
        return "library:read" if method in {"GET", "HEAD"} else "library:write"
    if path.startswith("/api/v1/audit"):
        return "audit:read"
    if path.startswith("/api/v1/backups"):
        # 备份清单/恢复预览包含目录结构与文件清单,创建备份是写操作;
        # 默认 token 不得读取备份内容,更不得触发全库备份。
        return "backup:read" if method in {"GET", "HEAD"} else "backup:write"
    if path.startswith("/api/v1/resources") and (
        path.endswith("/inspect") or "/inspect/" in path
    ):
        # 资源审查/批次读取是受控操作,读写都要求 review:write。
        return "review:write"
    if path.startswith("/api/v1/resource-search"):
        return "task:read"
    if path.startswith(("/api/v1/resources", "/api/v1/seasons")):
        return "library:read" if method in {"GET", "HEAD"} else "task:write"
    if path.startswith("/api/v1/sources/reliability"):
        return "library:read"
    if path.startswith("/api/v1/watchlist"):
        return "library:read"
    if path.startswith("/api/v1/webhooks"):
        # webhook 端点管理含密钥轮换/重试,写操作要求 settings:write。
        return "settings:read" if method in {"GET", "HEAD"} else "settings:write"
    if path.startswith("/api/v1/pwa"):
        return "task:read" if method in {"GET", "HEAD"} else "task:write"
    if path.startswith("/api/v1/cache"):
        return "system:read" if method in {"GET", "HEAD"} else "settings:write"
    if path.startswith("/api/v1/logs"):
        return "audit:read"
    if path.startswith("/api/v1/subtitles"):
        return "task:read" if method in {"GET", "HEAD"} else "task:write"
    if path.startswith(("/api/v1/subscriptions", "/api/v1/quality-profiles")):
        return "library:read" if method in {"GET", "HEAD"} else "task:write"
    # fail-closed:未知路径不静默降级到默认 scope。任何未来新增写端点
    # 漏配本清单都会在此拒绝,而不是悄悄获得 task:write 权限。
    raise AuthError(403, {"code": "scope_unmapped", "path": path})


def _decode_json_strings(value: str) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _xff_trusted() -> bool:
    """判断是否信任 X-Forwarded-For 头(部署在可信反代之后)。

    只有明确配置 X_FORWARDED_FOR_TRUSTED=1 才启用;未配置时一律忽略该头,
    防止客户端直连或未配置反代时伪造 IP 绕过按地址的限流。
    """
    return os.environ.get("X_FORWARDED_FOR_TRUSTED", "") == "1"


def _client_host(request: Request) -> str | None:
    # 反代部署下所有请求的 client.host 都是代理 IP,按 IP 限流会退化为
    # 全局共享桶(一次 5 次失败登录即可锁全实例)。仅在显式信任反代时
    # 取 X-Forwarded-For 首项作为真实客户端地址,其余情况仍用直连地址。
    if _xff_trusted():
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            host = forwarded.split(",", 1)[0].strip()
            if host and len(host) <= 64:
                return host
    host = request.client.host if request.client is not None else None
    return host if host and len(host) <= 64 else None


def _safe_client_version(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value[:64] if value else None


async def _emit_security_event(
    logger: Any | None,
    event: str,
    *,
    request: Request,
    actor_id: str | None,
) -> None:
    log_event = getattr(logger, "log_event", None)
    if not callable(log_event):
        return
    try:
        result = log_event(
            event,
            actor_type="agent",
            actor_id=actor_id,
            request_id=getattr(request.state, "request_id", None),
            correlation_id=getattr(request.state, "correlation_id", None),
            fields={"status": "accepted" if event.endswith("authenticated") else "denied"},
        )
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - audit must not change auth outcome
        return
