"""Bounded dynamic-link transport for the fixed p115client release."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from watch_assistant.adapters.p115_library_transport import (
    EXPECTED_P115CLIENT_VERSION,
    p115_readonly_timeout_executor,
)


class P115PlaybackTransportUnavailable(RuntimeError):
    """The fixed client cannot provide a safe dynamic link."""


P115_PLAYBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class P115PlaybackClient(Protocol):
    def download_url(self, pickcode: str, **kwargs: Any) -> object: ...


@dataclass(frozen=True, slots=True, repr=False)
class PlaybackLink:
    """Transient upstream link; its URL is never included in diagnostics."""

    url: str
    request_headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise P115PlaybackTransportUnavailable("invalid_dynamic_link")

    def __repr__(self) -> str:
        return "PlaybackLink(<redacted>)"


P115PlaybackCallExecutor = Callable[..., Any]


class P115PlaybackTransportProtocol(Protocol):
    async def download_url(
        self, pickcode: str, *, timeout_seconds: float
    ) -> PlaybackLink: ...


class P115FixedPlaybackTransport:
    """Expose only ``download_url`` and only transient safe request headers."""

    def __init__(
        self,
        client: P115PlaybackClient,
        *,
        call_executor: P115PlaybackCallExecutor = p115_readonly_timeout_executor,
    ) -> None:
        self._client = client
        self._call_executor = call_executor

    def __repr__(self) -> str:
        return "P115FixedPlaybackTransport(methods='download_url')"

    async def download_url(
        self, pickcode: str, *, timeout_seconds: float
    ) -> PlaybackLink:
        timeout = _positive_timeout(timeout_seconds)
        if not isinstance(pickcode, str) or not pickcode.strip():
            raise P115PlaybackTransportUnavailable("invalid_pickcode")
        pickcode = pickcode.strip()
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(
                    self._call_executor,
                    partial(
                        self._client.download_url,
                        user_agent=P115_PLAYBACK_USER_AGENT,
                    ),
                    pickcode,
                    timeout_seconds=timeout,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - 不得链起 p115client 原始异常,
            # 否则 logging.exception 的 traceback 会带出完整动态链接/签名参数
            # (违反凭据不进日志)。
            raise P115PlaybackTransportUnavailable("remote_failed") from None
        url = str(value)
        raw_headers = getattr(value, "headers", {})
        headers: list[tuple[str, str]] = []
        if isinstance(raw_headers, Mapping):
            for key, header_value in raw_headers.items():
                if (
                    isinstance(key, str)
                    and key.casefold() == "user-agent"
                    and isinstance(header_value, str)
                    and header_value
                    and len(header_value) <= 512
                ):
                    headers.append(("User-Agent", header_value))
        return PlaybackLink(url, tuple(headers))


def create_p115_playback_transport(
    credential: str,
) -> P115FixedPlaybackTransport:
    """Build one fixed-version client without making a remote request."""

    if not credential or _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        raise P115PlaybackTransportUnavailable("blocked_environment")
    try:
        from p115client import P115Client

        from watch_assistant.adapters.p115_request_profile import (
            ensure_browser_request_profile,
        )

        ensure_browser_request_profile()
        client = P115Client(credential, console_qrcode=False)
    except Exception:  # noqa: BLE001 - credential/client details stay private
        raise P115PlaybackTransportUnavailable("blocked_environment") from None
    return P115FixedPlaybackTransport(client)


def _positive_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise P115PlaybackTransportUnavailable("invalid_timeout")
    return float(value)


def _p115client_version() -> str | None:
    from importlib.metadata import version

    try:
        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay private
        return None


__all__ = [
    "P115_PLAYBACK_USER_AGENT",
    "P115FixedPlaybackTransport",
    "P115PlaybackClient",
    "P115PlaybackTransportProtocol",
    "P115PlaybackTransportUnavailable",
    "PlaybackLink",
    "create_p115_playback_transport",
]
