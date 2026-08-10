"""Live, scoped dynamic-link gateway for managed STRM entries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_playback_contract import (
    DynamicLinkOutcome,
    P115PlaybackGateway,
    PlaybackGate,
    PlaybackRequest,
    PlaybackStatus,
    evaluate_playback_gate,
    forwarding_policy,
)
from watch_assistant.adapters.p115_playback_transport import (
    P115PlaybackTransportProtocol,
    P115PlaybackTransportUnavailable,
    create_p115_playback_transport,
)
from watch_assistant.library_models import (
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)

DEFAULT_PLAYBACK_TIMEOUT_SECONDS = 30.0


class P115LivePlaybackGateway(P115PlaybackGateway):
    """Resolve only current, verified files inside a configured library scope."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        credential_source: Any,
        transport_factory: Callable[[str], P115PlaybackTransportProtocol]
        = create_p115_playback_transport,
        *,
        request_timeout_seconds: float = DEFAULT_PLAYBACK_TIMEOUT_SECONDS,
        max_concurrency: int = 2,
    ) -> None:
        if request_timeout_seconds <= 0 or max_concurrency < 1:
            raise ValueError("invalid_playback_configuration")
        self._session_factory = session_factory
        self._credential_source = credential_source
        self._transport_factory = transport_factory
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._transport: P115PlaybackTransportProtocol | None = None
        self._transport_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def __repr__(self) -> str:
        return "P115LivePlaybackGateway(mode='scoped_dynamic_link')"

    async def resolve(
        self,
        request: PlaybackRequest,
        *,
        gate: PlaybackGate,
        allowed_library_ids: Collection[str] | None = None,
    ) -> DynamicLinkOutcome:
        decision = evaluate_playback_gate(gate)
        if not decision.allowed:
            return DynamicLinkOutcome(
                PlaybackStatus.DISABLED
                if decision.error_code == "playback_disabled"
                else PlaybackStatus.UNVERIFIED,
                decision.error_code,
            )
        allowed = frozenset(allowed_library_ids or ())
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.manifest_id == request.manifest_id.value,
                    StrmManifestEntry.is_current.is_(True),
                    StrmManifestEntry.status == StrmManifestStatus.VERIFIED,
                )
            )
            if manifest is None:
                return DynamicLinkOutcome(
                    PlaybackStatus.NOT_FOUND, "manifest_out_of_scope"
                )
            library = await session.get(MediaLibrary, manifest.library_id)
            if (
                library is None
                or not library.enabled
                or not library.scope_verified
                or (allowed and library.id not in allowed)
                or not isinstance(manifest.cloud_file_id, str)
                or not manifest.cloud_file_id.isdigit()
                or not isinstance(manifest.pickcode, str)
                or not manifest.pickcode.strip()
            ):
                return DynamicLinkOutcome(
                    PlaybackStatus.NOT_FOUND, "manifest_out_of_scope"
                )
            pickcode = manifest.pickcode.strip()

        try:
            async with self._semaphore:
                link = await self._download_link(pickcode)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return DynamicLinkOutcome(PlaybackStatus.UNCERTAIN, "timeout")
        except P115PlaybackTransportUnavailable:
            return DynamicLinkOutcome(PlaybackStatus.FAILED, "remote_failed")
        except Exception:  # noqa: BLE001 - provider details never cross the boundary
            return DynamicLinkOutcome(PlaybackStatus.FAILED, "remote_failed")
        return DynamicLinkOutcome(
            PlaybackStatus.READY,
            url=link.url,
            policy=forwarding_policy(request),
            request_headers=link.request_headers,
        )

    async def _download_link(self, pickcode: str):
        transport = await self._get_transport()
        return await asyncio.wait_for(
            transport.download_url(
                pickcode, timeout_seconds=self._remaining_timeout()
            ),
            timeout=self._remaining_timeout(),
        )

    async def _get_transport(self) -> P115PlaybackTransportProtocol:
        if self._transport is not None:
            return self._transport
        async with self._transport_lock:
            if self._transport is not None:
                return self._transport
            try:
                credential = await asyncio.to_thread(self._credential_source.load)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 不链起底层异常:第三方错误文本可能包含凭据/pickcode/带参链接,
                # 一旦被调用方 logging.exception 记录即违反凭据不进日志规则。
                raise P115PlaybackTransportUnavailable("credentials_unavailable") from None
            if not credential:
                raise P115PlaybackTransportUnavailable("credentials_missing")
            try:
                transport = await asyncio.to_thread(
                    self._transport_factory, credential
                )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, P115PlaybackTransportUnavailable):
                raise
            except Exception:
                raise P115PlaybackTransportUnavailable("client_unavailable") from None
            self._transport = transport
            return transport

    def _remaining_timeout(self) -> float:
        return self._request_timeout_seconds


__all__ = ["DEFAULT_PLAYBACK_TIMEOUT_SECONDS", "P115LivePlaybackGateway"]
