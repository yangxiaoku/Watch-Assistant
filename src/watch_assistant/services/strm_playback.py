"""Short-lived, scoped dynamic-link reuse for the STRM playback boundary."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from time import monotonic

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
from watch_assistant.library_models import (
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)

DEFAULT_DYNAMIC_LINK_CACHE_TTL_SECONDS = 30.0
DEFAULT_DYNAMIC_LINK_CACHE_MAX_ENTRIES = 1024
PlaybackCacheValidator = Callable[
    [PlaybackRequest, Collection[str] | None], Awaitable[bool]
]


@dataclass(frozen=True, slots=True)
class _PlaybackCacheEntry:
    url: str
    request_headers: tuple[tuple[str, str], ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class _PlaybackCacheKey:
    manifest_id: str
    allowed_library_ids: tuple[str, ...] | None


class CachedStrmPlaybackGateway(P115PlaybackGateway):
    """Add bounded in-memory caching and per-scope single-flight resolution."""

    def __init__(
        self,
        gateway: P115PlaybackGateway,
        *,
        cache_validator: PlaybackCacheValidator,
        ttl_seconds: float = DEFAULT_DYNAMIC_LINK_CACHE_TTL_SECONDS,
        max_entries: int = DEFAULT_DYNAMIC_LINK_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("invalid_playback_cache_configuration")
        if not callable(cache_validator):
            raise TypeError("invalid_playback_cache_validator")
        self._gateway = gateway
        self._cache_validator = cache_validator
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[_PlaybackCacheKey, _PlaybackCacheEntry] = (
            OrderedDict()
        )
        self._inflight: dict[_PlaybackCacheKey, asyncio.Task[DynamicLinkOutcome]] = {}

    @property
    def upstream(self) -> P115PlaybackGateway:
        """Return the wrapped gateway for app-state replacement detection."""

        return self._gateway

    @property
    def cached_entry_count(self) -> int:
        """Expose only a count for diagnostics and offline tests."""

        return len(self._cache)

    async def resolve(
        self,
        request: PlaybackRequest,
        *,
        gate: PlaybackGate,
        allowed_library_ids: Collection[str] | None = None,
    ) -> DynamicLinkOutcome:
        decision = evaluate_playback_gate(gate)
        if not decision.allowed:
            return _gate_outcome(decision.error_code)

        key = _PlaybackCacheKey(
            request.manifest_id.value,
            _scope_key(allowed_library_ids),
        )
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                if cached.expires_at > self._clock():
                    if await self._cache_is_current(request, allowed_library_ids):
                        self._cache.move_to_end(key)
                        return _ready_outcome(request, cached)
                    self._cache.pop(key, None)
                self._cache.pop(key, None)

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._resolve_and_cache(
                        key,
                        request,
                        gate=gate,
                        allowed_library_ids=allowed_library_ids,
                    )
                )
                self._inflight[key] = task

        outcome = await asyncio.shield(task)
        return _with_request_policy(request, outcome)

    async def _cache_is_current(
        self,
        request: PlaybackRequest,
        allowed_library_ids: Collection[str] | None,
    ) -> bool:
        try:
            return await self._cache_validator(request, allowed_library_ids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cache validation fails closed
            return False

    async def _resolve_and_cache(
        self,
        key: _PlaybackCacheKey,
        request: PlaybackRequest,
        *,
        gate: PlaybackGate,
        allowed_library_ids: Collection[str] | None,
    ) -> DynamicLinkOutcome:
        current_task = asyncio.current_task()
        try:
            outcome = await self._gateway.resolve(
                request,
                gate=gate,
                allowed_library_ids=allowed_library_ids,
            )
            if outcome.status is PlaybackStatus.READY and outcome.url is not None:
                async with self._lock:
                    self._cache[key] = _PlaybackCacheEntry(
                        url=outcome.url,
                        request_headers=outcome.request_headers,
                        expires_at=self._clock() + self._ttl_seconds,
                    )
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
            return outcome
        finally:
            async with self._lock:
                if self._inflight.get(key) is current_task:
                    self._inflight.pop(key, None)


def _scope_key(
    allowed_library_ids: Collection[str] | None,
) -> tuple[str, ...] | None:
    if allowed_library_ids is None:
        return None
    return tuple(sorted(set(allowed_library_ids)))


def _gate_outcome(error_code: str | None) -> DynamicLinkOutcome:
    status = (
        PlaybackStatus.DISABLED
        if error_code == "playback_disabled"
        else PlaybackStatus.UNVERIFIED
    )
    return DynamicLinkOutcome(status, error_code)


def _ready_outcome(
    request: PlaybackRequest,
    entry: _PlaybackCacheEntry,
) -> DynamicLinkOutcome:
    return DynamicLinkOutcome(
        PlaybackStatus.READY,
        url=entry.url,
        policy=forwarding_policy(request),
        request_headers=entry.request_headers,
    )


def _with_request_policy(
    request: PlaybackRequest,
    outcome: DynamicLinkOutcome,
) -> DynamicLinkOutcome:
    if outcome.status is not PlaybackStatus.READY:
        return outcome
    return DynamicLinkOutcome(
        PlaybackStatus.READY,
        url=outcome.url,
        policy=forwarding_policy(request),
        request_headers=outcome.request_headers,
    )


async def validate_current_manifest_scope(
    session_factory: async_sessionmaker[AsyncSession],
    request: PlaybackRequest,
    allowed_library_ids: Collection[str] | None,
) -> bool:
    """Recheck revocation and library scope before serving a cached link."""

    allowed = frozenset(allowed_library_ids or ())
    try:
        async with session_factory() as session:
            manifest = await session.scalar(
                select(StrmManifestEntry).where(
                    StrmManifestEntry.manifest_id == request.manifest_id.value,
                    StrmManifestEntry.is_current.is_(True),
                    StrmManifestEntry.status == StrmManifestStatus.VERIFIED,
                )
            )
            if manifest is None:
                return False
            library = await session.get(MediaLibrary, manifest.library_id)
            return bool(
                library is not None
                and library.enabled
                and library.scope_verified
                and (not allowed or library.id in allowed)
                and manifest.cloud_file_id.isdigit()
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - validation fails closed without details
        return False


__all__ = [
    "DEFAULT_DYNAMIC_LINK_CACHE_MAX_ENTRIES",
    "DEFAULT_DYNAMIC_LINK_CACHE_TTL_SECONDS",
    "CachedStrmPlaybackGateway",
    "PlaybackCacheValidator",
    "validate_current_manifest_scope",
]
