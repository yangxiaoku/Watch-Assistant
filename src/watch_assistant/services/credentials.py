"""Authenticated storage and runtime application of managed credentials."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115 import P115Adapter
from watch_assistant.adapters.tmdb import TmdbAuthError, TmdbClient
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import ApplicationSettings
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.p115_credentials import (
    CompositeCookieProvider,
    CookieProvider,
    normalize_cookie_text,
)
from watch_assistant.services.settings import shared_settings_mutation_lock

SETTINGS_ID = "default"
_TMDB_KEY = re.compile(r"^[^\x00-\x1f\x7f\r\n]{1,256}$")


class CredentialConflict(Exception):
    pass


class CredentialRateLimited(Exception):
    pass


class CredentialRejected(Exception):
    pass


class CredentialValidationUnavailable(Exception):
    pass


logger = logging.getLogger(__name__)


class CredentialService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        *,
        environment_tmdb_key: str,
        fallback_cookie_provider: CookieProvider,
        timeout_seconds: float = 10.0,
        tmdb_client: TmdbClient | None = None,
        p115_adapter: P115Adapter | None = None,
        cookie_provider: CompositeCookieProvider | None = None,
        event_logger: EventLogger | None = None,
        runtime_state: Any | None = None,
        p115_runtime_callback: Callable[[bool], Awaitable[None]] | None = None,
        mutation_lock: asyncio.Lock | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session_factory = session_factory
        self._crypto = crypto
        self._environment_tmdb_key = environment_tmdb_key
        self._fallback_cookie_provider = fallback_cookie_provider
        self._cookie_provider = cookie_provider or CompositeCookieProvider(
            fallback_cookie_provider
        )
        self._tmdb_client = tmdb_client
        self._p115_adapter = p115_adapter
        self._event_logger = event_logger
        self._runtime_state = runtime_state
        self._p115_runtime_callback = p115_runtime_callback
        self._timeout_seconds = timeout_seconds
        self._operation_lock = asyncio.Lock()
        self._mutation_lock = mutation_lock or shared_settings_mutation_lock(
            session_factory
        )
        self._rate_windows: dict[str, deque[datetime]] = {}

    def bind_runtime(
        self,
        *,
        tmdb_client: TmdbClient,
        p115_adapter: P115Adapter | None,
        runtime_state: Any | None = None,
    ) -> None:
        self._tmdb_client = tmdb_client
        self._p115_adapter = p115_adapter
        if runtime_state is not None:
            self._runtime_state = runtime_state

    def check_rate_limit(
        self,
        identity: str,
        *,
        now: datetime | None = None,
        limit: int = 8,
        window: timedelta = timedelta(minutes=1),
    ) -> None:
        current = now or datetime.now(UTC)
        bucket = self._rate_windows.setdefault(identity, deque())
        cutoff = current - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise CredentialRateLimited
        bucket.append(current)

    async def load_managed(self) -> tuple[str | None, str | None]:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            tmdb = self._decrypt(settings.managed_tmdb_key_encrypted)
            cookie = self._decrypt(settings.managed_p115_cookie_encrypted)
        if cookie is not None:
            normalized = normalize_cookie_text(cookie)
            cookie = normalized
        self._cookie_provider.set_managed(cookie)
        return tmdb, cookie

    async def snapshot(self) -> dict[str, object]:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            tmdb_configured = (
                self._decrypt(settings.managed_tmdb_key_encrypted) is not None
            )
            cookie = self._decrypt(settings.managed_p115_cookie_encrypted)
            cookie_configured = cookie is not None
            revision = settings.revision
            tmdb_updated = settings.managed_tmdb_updated_at
            cookie_updated = settings.managed_p115_updated_at
        if tmdb_updated is not None and tmdb_updated.tzinfo is None:
            tmdb_updated = tmdb_updated.replace(tzinfo=UTC)
        if cookie_updated is not None and cookie_updated.tzinfo is None:
            cookie_updated = cookie_updated.replace(tzinfo=UTC)
        try:
            fallback = self._fallback_cookie_provider.load() is not None
        except Exception:  # noqa: BLE001 - local fallback fails closed
            fallback = False
        try:
            active_cookie = self._cookie_provider.load()
        except Exception:  # noqa: BLE001 - local fallback fails closed
            active_cookie = None
        runtime_ready = bool(
            getattr(self._runtime_state, "p115_ready", False)
            if self._runtime_state is not None
            else False
        )
        return {
            "revision": revision,
            "tmdb": {
                "configured": tmdb_configured or bool(self._environment_tmdb_key),
                "source": "managed" if tmdb_configured else "environment",
                "last_updated_at": tmdb_updated,
            },
            "p115_cookie": {
                "configured": cookie_configured or fallback,
                "source": "managed" if cookie_configured else "file",
                "last_updated_at": cookie_updated,
                "structure_valid": active_cookie is not None,
                "ready": runtime_ready and active_cookie is not None,
            },
        }

    async def update_tmdb(self, value: str, revision: int) -> dict[str, object]:
        if not isinstance(value, str) or _TMDB_KEY.fullmatch(value) is None:
            raise CredentialRejected
        async with self._operation_lock:
            await self._assert_revision(revision)
            await self._validate_tmdb(value)
            now = datetime.now(UTC)
            async with self._mutation_lock, self._session_factory() as session:
                settings = await self._get_or_create(session)
                if settings.revision != revision:
                    raise CredentialConflict
                settings.managed_tmdb_key_encrypted = self._crypto.encrypt(value)
                settings.managed_tmdb_updated_at = now
                settings.revision += 1
                commit_cancelled = await self._commit_uncancellable(session)
                next_revision = settings.revision
            cancelled = await self._finish_after_commit(self._apply_tmdb(value))
            if commit_cancelled or cancelled:
                raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "tmdb"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def update_p115_cookie(self, value: str, revision: int) -> dict[str, object]:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 16 * 1024:
            raise CredentialRejected
        normalized = normalize_cookie_text(value)
        if normalized is None:
            raise CredentialRejected
        async with self._operation_lock:
            await self._assert_revision(revision)
            await self._validate_p115(normalized)
            now = datetime.now(UTC)
            async with self._mutation_lock, self._session_factory() as session:
                settings = await self._get_or_create(session)
                if settings.revision != revision:
                    raise CredentialConflict
                settings.managed_p115_cookie_encrypted = self._crypto.encrypt(
                    normalized
                )
                settings.managed_p115_updated_at = now
                settings.revision += 1
                commit_cancelled = await self._commit_uncancellable(session)
                next_revision = settings.revision
            cancelled = await self._finish_after_commit(
                self._apply_p115_runtime(normalized, validate=False),
                fail_closed_p115=True,
            )
            if commit_cancelled or cancelled:
                raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "p115_cookie"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def reset_tmdb(self, revision: int) -> dict[str, object]:
        async with self._operation_lock:
            next_revision, commit_cancelled = await self._reset("tmdb", revision)
            cancelled = await self._finish_after_commit(self._apply_tmdb(None))
            if commit_cancelled or cancelled:
                raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "tmdb_reset"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def reset_p115_cookie(self, revision: int) -> dict[str, object]:
        async with self._operation_lock:
            next_revision, commit_cancelled = await self._reset("p115", revision)
            cancelled = await self._finish_after_commit(
                self._apply_p115_runtime(None, validate=True),
                fail_closed_p115=True,
            )
            if commit_cancelled or cancelled:
                raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "p115_reset"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def _validate_tmdb(self, value: str) -> None:
        client = self._tmdb_client
        if client is None:
            raise CredentialValidationUnavailable
        try:
            method = getattr(client, "validate_api_key", None)
            if not callable(method):
                raise CredentialValidationUnavailable
            await asyncio.wait_for(method(value), timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TmdbAuthError:
            raise CredentialRejected from None
        except CredentialValidationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - stable public error only
            if exc.__class__.__name__ in {"AuthError", "CredentialRejected"}:
                raise CredentialRejected from None
            raise CredentialValidationUnavailable from None

    async def _validate_p115(self, value: str) -> None:
        adapter = self._p115_adapter
        if adapter is None:
            raise CredentialValidationUnavailable
        try:
            method = getattr(adapter, "validate_cookie", None)
            if not callable(method):
                raise CredentialValidationUnavailable
            await asyncio.wait_for(method(value), timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            raise
        except CredentialRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - stable public error only
            if exc.__class__.__name__ in {"P115NeedsAuthError", "AuthError"}:
                raise CredentialRejected from None
            if exc.__class__.__name__ == "P115UnavailableError":
                raise CredentialValidationUnavailable from None
            raise CredentialValidationUnavailable from None

    async def _apply_tmdb(self, value: str | None) -> None:
        if self._tmdb_client is None:
            return
        self._tmdb_client.set_api_key(value or self._environment_tmdb_key)

    async def _apply_p115_runtime(self, cookie: str | None, *, validate: bool) -> None:
        adapter = self._p115_adapter
        if adapter is None:
            return
        ready = False
        if cookie is not None:
            self._cookie_provider.set_managed(cookie)
        else:
            self._cookie_provider.set_managed(None)
        if validate:
            fallback = self._safe_fallback_cookie()
            if fallback is None:
                await self._set_p115_runtime(False)
                return
            try:
                await self._validate_p115(fallback)
            except (CredentialRejected, CredentialValidationUnavailable):
                await self._set_p115_runtime(False)
                return
        try:
            ready = await adapter.ensure_available()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - readiness fails closed
            ready = False
        await self._set_p115_runtime(bool(ready))

    async def _set_p115_runtime(self, ready: bool) -> None:
        if self._runtime_state is not None:
            self._runtime_state.p115_ready = ready
            self._runtime_state.push_capabilities = {
                "magnet": ready,
                "share": False,
            }
        if self._p115_runtime_callback is not None:
            try:
                await self._p115_runtime_callback(ready)
            except asyncio.CancelledError:
                if self._runtime_state is not None:
                    self._runtime_state.p115_ready = False
                    self._runtime_state.push_capabilities = {
                        "magnet": False,
                        "share": False,
                    }
                raise
            except Exception:
                # 回调(如 apply_dirty_runtime 启动 dirty worker)失败被吞掉会让
                # 后台组件静默缺席且无任何日志。记录堆栈以便诊断:例如 STRM 自动
                # 增量队列积压数天而 worker 从未启动的情况。
                logger.exception(
                    "p115 runtime callback failed; p115 marked not ready"
                )
                if self._runtime_state is not None:
                    self._runtime_state.p115_ready = False
                    self._runtime_state.push_capabilities = {
                        "magnet": False,
                        "share": False,
                    }

    def _safe_fallback_cookie(self) -> str | None:
        try:
            return self._fallback_cookie_provider.load()
        except Exception:  # noqa: BLE001 - fallback state is opaque
            return None

    async def _commit_uncancellable(self, session: AsyncSession) -> bool:
        commit_task = asyncio.create_task(session.commit())
        cancelled = False
        while not commit_task.done():
            try:
                await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                cancelled = True
        await commit_task
        return cancelled

    async def _finish_after_commit(
        self, operation: Awaitable[None], *, fail_closed_p115: bool = False
    ) -> bool:
        task = asyncio.create_task(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        operation_cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            operation_cancelled = True
            if fail_closed_p115:
                await self._set_p115_runtime(False)
        except Exception:  # noqa: BLE001 - post-commit convergence is opaque
            if fail_closed_p115:
                await self._set_p115_runtime(False)
        return cancelled or operation_cancelled

    async def _reset(self, kind: str, revision: int) -> tuple[int, bool]:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != revision:
                raise CredentialConflict
            if kind == "tmdb":
                settings.managed_tmdb_key_encrypted = None
                settings.managed_tmdb_updated_at = None
            else:
                settings.managed_p115_cookie_encrypted = None
                settings.managed_p115_updated_at = None
            settings.revision += 1
            commit_cancelled = await self._commit_uncancellable(session)
            return settings.revision, commit_cancelled

    async def _assert_revision(self, revision: int) -> None:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != revision:
                raise CredentialConflict

    async def _get_or_create(self, session: AsyncSession) -> ApplicationSettings:
        settings = await session.get(ApplicationSettings, SETTINGS_ID)
        if settings is None:
            settings = ApplicationSettings(id=SETTINGS_ID)
            session.add(settings)
            await session.commit()
        return settings

    def _decrypt(self, encrypted: str | None) -> str | None:
        if not encrypted:
            return None
        try:
            return self._crypto.decrypt(encrypted)
        except Exception:  # noqa: BLE001 - managed secret failure falls back
            return None
