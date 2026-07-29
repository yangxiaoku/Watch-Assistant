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


class P115ReadOnlyTransportUnavailable(RuntimeError):
    """The fixed client cannot enforce the required read-only boundary."""


class P115FixedReadOnlyTransport:
    """Expose only bounded ``fs_files`` and ``fs_info`` calls."""

    def __init__(
        self,
        client: P115ReadOnlyClient,
        *,
        call_executor: P115ReadOnlyCallExecutor,
    ) -> None:
        self._client = client
        self._call_executor = call_executor

    def __repr__(self) -> str:
        return "P115FixedReadOnlyTransport(methods='fs_files,fs_info')"

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
