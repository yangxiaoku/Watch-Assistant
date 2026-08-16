"""Bounded fixed-client transport for the read-only P115 library gateway."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from importlib.metadata import version
from typing import Any, Protocol

EXPECTED_P115CLIENT_VERSION = "0.0.9.6.5.1"
P115_BUSY_OPERATION_ERRNO = 990009
P115_BUSY_OPERATION_RETRY_DELAY_SECONDS = 3.0

# 进程级 115 请求节流:实测(2026-08-14)持续密集请求会触发账号级风控
# (/files、/info 接口 405/429,持续数分钟到数十分钟);正常扫描/观察/核对
# 请求按最小间隔节流,避免触发风控。只作用于真实 HTTP 请求
# (request hook 内),测试 fake 客户端不受影响。
# 0.15s 与批量页 50 的已验证节奏一致(e6eb6dd:相邻请求 0.15s 间隔实测
# 安全);0.5s 是逐页 1 条时代的保守值,批量页下会让 38 条判型花 19s 纯
# 节流时间,逼近网关 30s deadline 导致扫描确定性失败(2026-08-16 实测)。
READ_THROTTLE_SECONDS = 0.15
_last_read_at = 0.0


def throttle_read() -> None:
    """在所有真实 115 HTTP 请求前按最小间隔节流(进程共享)。"""

    global _last_read_at
    now = time.monotonic()
    wait = READ_THROTTLE_SECONDS - (now - _last_read_at)
    if wait > 0:
        time.sleep(wait)
    _last_read_at = time.monotonic()


class P115ReadOnlyClient(Protocol):
    def fs_files(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...

    def fs_info(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_files_app(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...

    def fs_info_app(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...


P115ReadOnlyCallExecutor = Callable[..., Any]


class P115ReadOnlyTransportProtocol(Protocol):
    async def fs_files(
        self, payload: Mapping[str, int | str], *, timeout_seconds: float
    ) -> object: ...

    async def fs_info(
        self, payload: Mapping[str, str], *, timeout_seconds: float
    ) -> object: ...

    async def fs_files_app(
        self, payload: Mapping[str, int | str], *, timeout_seconds: float
    ) -> object: ...

    async def fs_info_app(
        self, payload: Mapping[str, str], *, timeout_seconds: float
    ) -> object: ...


class P115ReadOnlyTransportUnavailable(RuntimeError):
    """The fixed client cannot enforce the required read-only boundary."""


class P115FixedReadOnlyTransport:
    """Expose only bounded ``fs_files``/``fs_info`` and their app variants."""

    def __init__(
        self,
        client: P115ReadOnlyClient,
        *,
        call_executor: P115ReadOnlyCallExecutor,
    ) -> None:
        self._client = client
        self._call_executor = call_executor

    def __repr__(self) -> str:
        return (
            "P115FixedReadOnlyTransport("
            "methods='fs_files,fs_info,fs_files_app,fs_info_app')"
        )

    async def fs_files(
        self, payload: Mapping[str, int | str], *, timeout_seconds: float
    ) -> object:
        try:
            response = await self._call(
                self._client.fs_files, payload, timeout_seconds=timeout_seconds
            )
            if not _is_structured_method_not_allowed(response):
                return response
        except Exception as error:
            if not _is_method_not_allowed(error):
                raise
        fallback = getattr(self._client, "fs_files_app", None)
        if not callable(fallback):
            raise P115ReadOnlyTransportUnavailable("app_read_endpoint_unavailable")
        return await self._call(fallback, payload, timeout_seconds=timeout_seconds)

    async def fs_info(
        self, payload: Mapping[str, str], *, timeout_seconds: float
    ) -> object:
        try:
            response = await self._call(
                self._client.fs_info, payload, timeout_seconds=timeout_seconds
            )
            if not _is_structured_method_not_allowed(response):
                return response
        except Exception as error:
            if not _is_method_not_allowed(error):
                raise
        fallback = getattr(self._client, "fs_info_app", None)
        if not callable(fallback):
            raise P115ReadOnlyTransportUnavailable("app_read_endpoint_unavailable")
        return await self._call(fallback, payload, timeout_seconds=timeout_seconds)

    async def fs_files_app(
        self, payload: Mapping[str, int | str], *, timeout_seconds: float
    ) -> object:
        """Forward to the proapi app listing endpoint without fallback."""

        return await self._call(
            self._client.fs_files_app, payload, timeout_seconds=timeout_seconds
        )

    async def fs_info_app(
        self, payload: Mapping[str, str], *, timeout_seconds: float
    ) -> object:
        """Forward to the proapi app detail endpoint without fallback."""

        return await self._call(
            self._client.fs_info_app, payload, timeout_seconds=timeout_seconds
        )

    async def _call(
        self,
        method: Callable[..., Any],
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> object:
        timeout = _positive_timeout(timeout_seconds)
        return await asyncio.wait_for(
            asyncio.to_thread(
                self._call_executor,
                method,
                dict(payload),
                timeout_seconds=timeout,
            ),
            timeout=timeout,
        )


def create_p115_readonly_transport(credential: str) -> P115FixedReadOnlyTransport:
    """Build one fixed-version client without making a remote request."""

    if not credential or _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        raise P115ReadOnlyTransportUnavailable("blocked_environment")
    try:
        from p115client import P115Client

        from watch_assistant.adapters.p115_request_profile import (
            ensure_browser_request_profile,
        )

        ensure_browser_request_profile()
        client = P115Client(credential, console_qrcode=False)
    except Exception:  # noqa: BLE001 - credentials and client details stay private
        raise P115ReadOnlyTransportUnavailable("blocked_environment") from None
    return P115FixedReadOnlyTransport(
        client, call_executor=p115_readonly_timeout_executor
    )


def p115_readonly_timeout_executor(
    method: Callable[..., Any],
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> object:
    """Use the documented request hook with timeout and retries disabled."""

    timeout = _positive_timeout(timeout_seconds)
    try:
        from urllib3_future_request import request as urllib3_request
    except ImportError:
        raise P115ReadOnlyTransportUnavailable("blocked_environment") from None

    def request_with_timeout(*, async_: bool = False, **request_kwargs: Any) -> Any:
        if async_:
            raise P115ReadOnlyTransportUnavailable("blocked_environment")
        throttle_read()
        request_kwargs["timeout"] = timeout
        request_kwargs["retries"] = False
        return urllib3_request(async_=False, **request_kwargs)

    for attempt in range(2):
        try:
            return method(payload, async_=False, request=request_with_timeout)
        except Exception as error:
            if attempt or not _has_busy_errno(error):
                raise
            time.sleep(P115_BUSY_OPERATION_RETRY_DELAY_SECONDS)
    raise P115ReadOnlyTransportUnavailable("blocked_environment")


def _has_busy_errno(error: BaseException) -> bool:
    if getattr(error, "errno", None) == P115_BUSY_OPERATION_ERRNO:
        return True
    return any(
        isinstance(argument, Mapping)
        and argument.get("errno") == P115_BUSY_OPERATION_ERRNO
        for argument in getattr(error, "args", ())
    )


def _is_method_not_allowed(error: BaseException) -> bool:
    """Use the app read endpoint only for a provider-level HTTP 405."""

    for name in ("status", "status_code", "code"):
        value = getattr(error, name, None)
        if value == 405:
            return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 405


def _is_structured_method_not_allowed(response: object) -> bool:
    if not isinstance(response, Mapping):
        return False
    return any(
        response.get(name) in {405, "405"} for name in ("status_code", "http_status")
    )


def _positive_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise P115ReadOnlyTransportUnavailable("blocked_environment")
    return float(value)


def _p115client_version() -> str | None:
    try:
        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay behind the boundary
        return None


__all__ = [
    "EXPECTED_P115CLIENT_VERSION",
    "P115FixedReadOnlyTransport",
    "P115ReadOnlyCallExecutor",
    "P115ReadOnlyTransportProtocol",
    "P115ReadOnlyTransportUnavailable",
    "create_p115_readonly_transport",
    "p115_readonly_timeout_executor",
]
