"""Managed Prowlarr configuration and runtime client replacement."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.prowlarr import (
    ProwlarrAuthError,
    ProwlarrClient,
    ProwlarrError,
    ProwlarrInvalidResponseError,
)
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import ApplicationSettings
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.settings import shared_settings_mutation_lock
from watch_assistant.services.source_health import (
    SourceHealthState,
    SourceHealthTracker,
    health_message,
    health_reason_code,
    health_reason_message,
)

SETTINGS_ID = "default"
_API_KEY = re.compile(r"^[^\x00-\x1f\x7f\r\n]{1,512}$")
_VERIFY_QUERY = "Watch Assistant connection check"


class ProwlarrSettingsConflict(Exception):
    pass


class ProwlarrSettingsRateLimited(Exception):
    pass


class ProwlarrSettingsRejected(Exception):
    pass


class ProwlarrSettingsUnavailable(Exception):
    pass


class ProwlarrSettingsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        *,
        environment_enabled: bool = False,
        environment_base_url: str = "",
        environment_api_key: str = "",
        timeout_seconds: float = 12.0,
        event_logger: EventLogger | None = None,
        runtime_state: Any | None = None,
        mutation_lock: asyncio.Lock | None = None,
        hostname_resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._environment_enabled = environment_enabled
        self._environment_base_url = environment_base_url
        self._environment_api_key = environment_api_key
        self._timeout_seconds = timeout_seconds
        self._event_logger = event_logger
        self._runtime_state = runtime_state
        self._hostname_resolver = (
            hostname_resolver if hostname_resolver is not None else _resolve_hostname
        )
        self._operation_lock = asyncio.Lock()
        self._mutation_lock = mutation_lock or shared_settings_mutation_lock(
            session_factory
        )
        self._rate_windows: dict[str, deque[datetime]] = {}
        self._health = SourceHealthTracker()

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
            raise ProwlarrSettingsRateLimited
        bucket.append(current)

    async def snapshot(self) -> dict[str, object]:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            values = self._effective_values(settings)
        response = {
            "source": values["source"],
            "enabled": values["enabled"],
            "configured": values["configured"],
            "base_url": values["base_url"],
            "api_key_configured": values["api_key"] is not None,
            "api_key_source": values["api_key_source"],
            "last_updated_at": values["updated_at"],
            "revision": values["revision"],
        }
        response.update(self._health_fields(values))
        return response

    async def runtime_client(self) -> ProwlarrClient | None:
        async with self._mutation_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            values = self._effective_values(settings)
        return self._new_client(values)

    async def update(
        self,
        *,
        enabled: bool | None,
        base_url: str | None,
        api_key: str | None,
        fields_set: set[str],
        revision: int,
    ) -> dict[str, object]:
        async with self._operation_lock:
            commit_cancelled = False
            async with self._mutation_lock, self._session_factory() as session:
                settings = await self._get_or_create(session)
                if settings.revision != revision:
                    raise ProwlarrSettingsConflict
                next_revision = settings.revision
                if "base_url" in fields_set:
                    settings.managed_prowlarr_base_url = _normalize_base_url(
                        base_url, hostname_resolver=self._hostname_resolver
                    )
                if "api_key" in fields_set:
                    if api_key is None or _API_KEY.fullmatch(api_key) is None:
                        raise ProwlarrSettingsRejected
                    settings.managed_prowlarr_api_key_encrypted = self._crypto.encrypt(
                        api_key
                    )
                if "enabled" in fields_set:
                    settings.managed_prowlarr_enabled = enabled
                if fields_set:
                    settings.managed_prowlarr_updated_at = datetime.now(UTC)
                    settings.revision += 1
                    try:
                        await self._commit_uncancellable(session)
                    except asyncio.CancelledError:
                        commit_cancelled = True
                    next_revision = settings.revision
            runtime_cancelled = await self._apply_runtime_consistently()
            if fields_set:
                self._health.reset(
                    configured=bool(self._effective_values(settings)["configured"])
                )
        if commit_cancelled or runtime_cancelled:
            raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "prowlarr"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def reset(self, revision: int) -> dict[str, object]:
        async with self._operation_lock:
            commit_cancelled = False
            async with self._mutation_lock, self._session_factory() as session:
                settings = await self._get_or_create(session)
                if settings.revision != revision:
                    raise ProwlarrSettingsConflict
                settings.managed_prowlarr_enabled = None
                settings.managed_prowlarr_base_url = None
                settings.managed_prowlarr_api_key_encrypted = None
                settings.managed_prowlarr_updated_at = None
                settings.revision += 1
                try:
                    await self._commit_uncancellable(session)
                except asyncio.CancelledError:
                    commit_cancelled = True
                next_revision = settings.revision
            runtime_cancelled = await self._apply_runtime_consistently()
            self._health.reset(configured=False)
        if commit_cancelled or runtime_cancelled:
            raise asyncio.CancelledError
        await emit_event(
            self._event_logger, "settings.changed", fields={"status": "prowlarr_reset"}
        )
        return await self.snapshot() | {"revision": next_revision}

    async def verify(self) -> dict[str, object]:
        checked_at = datetime.now(UTC)
        snapshot = await self.snapshot()
        if not snapshot["enabled"]:
            return {
                "source": "prowlarr",
                "status": "disabled",
                "configured": False,
                "base_url": snapshot["base_url"],
                "message_code": "prowlarr_disabled",
                "checked_at": checked_at,
                "state": SourceHealthState.DISABLED,
                "message_zh": health_message(SourceHealthState.DISABLED),
                "reason_code": "prowlarr_disabled",
                "reason_zh": health_message(SourceHealthState.DISABLED),
            }
        if not snapshot["configured"]:
            return {
                "source": "prowlarr",
                "status": "unavailable",
                "configured": False,
                "base_url": snapshot["base_url"],
                "message_code": "prowlarr_not_configured",
                "checked_at": checked_at,
                "state": SourceHealthState.NOT_CONFIGURED,
                "message_zh": health_message(SourceHealthState.NOT_CONFIGURED),
                "reason_code": "prowlarr_not_configured",
                "reason_zh": health_message(SourceHealthState.NOT_CONFIGURED),
            }
        client = await self.runtime_client()
        if client is None:
            raise ProwlarrSettingsUnavailable
        try:
            await asyncio.wait_for(client.search(_VERIFY_QUERY), self._timeout_seconds)
        except asyncio.CancelledError:
            raise
        except ProwlarrAuthError:
            return self._verify_result(snapshot, "prowlarr_auth_required", checked_at)
        except ProwlarrInvalidResponseError:
            return self._verify_result(
                snapshot, "prowlarr_invalid_response", checked_at
            )
        except ProwlarrError as exc:
            return self._verify_result(
                snapshot, _public_error_code(exc.error_code), checked_at
            )
        except TimeoutError:
            self._health.record_failure("prowlarr_timeout")
            return self._verify_result(snapshot, "prowlarr_unavailable", checked_at)
        finally:
            await client.aclose()
        return {
            "source": "prowlarr",
            "status": "available",
            "configured": True,
            "base_url": snapshot["base_url"],
            "message_code": None,
            "checked_at": checked_at,
            "state": SourceHealthState.AVAILABLE,
            "message_zh": health_message(SourceHealthState.AVAILABLE),
            "reason_code": "prowlarr_available",
            "reason_zh": health_message(SourceHealthState.AVAILABLE),
        }

    async def _apply_runtime(self) -> None:
        if self._runtime_state is None:
            return
        search_service = getattr(self._runtime_state, "search_service", None)
        replace = getattr(search_service, "replace_prowlarr_client", None)
        if not callable(replace):
            return
        await replace(await self.runtime_client())

    async def _apply_runtime_consistently(self) -> bool:
        """Finish a committed runtime replacement before returning cancellation."""
        runtime_task = asyncio.create_task(self._apply_runtime())
        cancelled = False
        while not runtime_task.done():
            try:
                await asyncio.shield(runtime_task)
            except asyncio.CancelledError:
                cancelled = True
        await runtime_task
        return cancelled or bool(asyncio.current_task().cancelling())

    def _effective_values(self, settings: ApplicationSettings) -> dict[str, object]:
        managed_key = self._decrypt(settings.managed_prowlarr_api_key_encrypted)
        managed_url = _safe_normalize_base_url(
            settings.managed_prowlarr_base_url,
            hostname_resolver=self._hostname_resolver,
        )
        environment_url = _safe_normalize_base_url(
            self._environment_base_url,
            hostname_resolver=self._hostname_resolver,
        )
        base_url = managed_url or environment_url
        api_key = managed_key or self._environment_api_key or None
        enabled = (
            settings.managed_prowlarr_enabled
            if settings.managed_prowlarr_enabled is not None
            else self._environment_enabled
        )
        managed = any(
            value is not None
            for value in (
                settings.managed_prowlarr_enabled,
                settings.managed_prowlarr_base_url,
                settings.managed_prowlarr_api_key_encrypted,
            )
        )
        environment = bool(
            self._environment_enabled
            or self._environment_base_url
            or self._environment_api_key
        )
        return {
            "source": (
                "managed" if managed else "environment" if environment else "none"
            ),
            "api_key_source": (
                "managed"
                if managed_key
                else "environment"
                if self._environment_api_key
                else "none"
            ),
            "enabled": bool(enabled),
            "configured": bool(enabled and base_url and api_key),
            "base_url": base_url,
            "api_key": api_key,
            "updated_at": _as_utc(settings.managed_prowlarr_updated_at),
            "revision": settings.revision,
        }

    def _new_client(self, values: dict[str, object]) -> ProwlarrClient | None:
        if not values["configured"]:
            return None
        return ProwlarrClient(
            str(values["base_url"]),
            str(values["api_key"]),
            timeout=self._timeout_seconds,
            health_tracker=self._health,
        )

    def _verify_result(
        self, snapshot: dict[str, object], message_code: str, checked_at: datetime
    ) -> dict[str, object]:
        health = self._health.snapshot()
        return {
            "source": "prowlarr",
            "status": "unavailable",
            "configured": True,
            "base_url": snapshot["base_url"],
            "message_code": message_code,
            "checked_at": checked_at,
            "state": health.state,
            "message_zh": health_message(health.state),
            "retry_after_seconds": health.retry_after_seconds,
            "reason_code": health_reason_code(health.state, health.last_error_code),
            "reason_zh": health_reason_message(
                health.state, health.last_error_code
            ),
        }

    def _health_fields(self, values: dict[str, object]) -> dict[str, object]:
        last_error_code: str | None = None
        if not values["enabled"]:
            state = SourceHealthState.DISABLED
            message_code = "prowlarr_disabled"
            checked_at = None
            retry_after = None
            failures = 0
        elif not values["configured"]:
            state = SourceHealthState.NOT_CONFIGURED
            message_code = "prowlarr_not_configured"
            checked_at = None
            retry_after = None
            failures = 0
        else:
            health = self._health.snapshot()
            state = health.state
            if state in {SourceHealthState.DISABLED, SourceHealthState.NOT_CONFIGURED}:
                state = SourceHealthState.UNVERIFIED
            message_code = _public_error_code(health.last_error_code)
            checked_at = health.checked_at
            retry_after = health.retry_after_seconds
            failures = health.consecutive_failures
            last_error_code = health.last_error_code
        return {
            "health_state": state,
            "health_message_code": message_code,
            "health_message_zh": health_message(state),
            "health_reason_code": health_reason_code(
                state,
                last_error_code,
            ),
            "health_reason_zh": health_reason_message(state, last_error_code),
            "health_checked_at": checked_at,
            "health_retry_after_seconds": retry_after,
            "health_consecutive_failures": failures,
        }

    async def _commit_uncancellable(self, session: AsyncSession) -> None:
        commit_task = asyncio.create_task(session.commit())
        cancelled = False
        while not commit_task.done():
            try:
                await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                cancelled = True
        await commit_task
        if cancelled or bool(asyncio.current_task().cancelling()):
            raise asyncio.CancelledError

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


def _normalize_base_url(
    value: object,
    *,
    hostname_resolver: Callable[[str], Iterable[str]] | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProwlarrSettingsRejected
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ProwlarrSettingsRejected from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ProwlarrSettingsRejected
    resolver = (
        hostname_resolver if hostname_resolver is not None else _resolve_hostname
    )
    _validate_public_hostname(hostname, resolver)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, "", ""))


def _safe_normalize_base_url(
    value: object,
    *,
    hostname_resolver: Callable[[str], Iterable[str]] | None = None,
) -> str | None:
    try:
        return _normalize_base_url(value, hostname_resolver=hostname_resolver)
    except ProwlarrSettingsRejected:
        return None


def _resolve_hostname(hostname: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        addresses = tuple(info[4][0] for info in infos if info[4])
    except (OSError, TypeError, ValueError, IndexError):
        raise ProwlarrSettingsRejected from None
    if not addresses:
        raise ProwlarrSettingsRejected
    return addresses


def _validate_public_hostname(
    hostname: str,
    hostname_resolver: Callable[[str], Iterable[str]],
) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            resolved_addresses = tuple(hostname_resolver(hostname))
        except Exception:  # noqa: BLE001 - resolver failures fail closed
            raise ProwlarrSettingsRejected from None
        if not resolved_addresses:
            raise ProwlarrSettingsRejected
        for resolved_address in resolved_addresses:
            _validate_public_address(resolved_address)
        return
    _validate_public_address(address)


def _validate_public_address(value: object) -> None:
    try:
        address = (
            value
            if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(value)
        )
    except (TypeError, ValueError):
        raise ProwlarrSettingsRejected from None
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    ):
        raise ProwlarrSettingsRejected


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _public_error_code(error_code: str | None) -> str | None:
    """Map internal failure classes to the existing safe API catalog."""
    if error_code in {"prowlarr_auth_required", "prowlarr_invalid_response"}:
        return error_code
    if error_code:
        return "prowlarr_unavailable"
    return None
