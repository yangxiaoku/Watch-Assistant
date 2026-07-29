"""Offline-only C05 playback contract; no client, credential, or transport use.

The dynamic link is deliberately transient and redacted from every diagnostic
DTO.  This module does not provide an HTTP route or persist a link.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PlaybackContractError(ValueError):
    """A playback request fell outside the frozen offline contract."""


class PlaybackMethod(StrEnum):
    HEAD = "HEAD"
    GET = "GET"


class PlaybackStatus(StrEnum):
    READY = "ready"
    DISABLED = "disabled"
    UNVERIFIED = "unverified"
    NOT_FOUND = "not_found"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


_MANIFEST_ID = re.compile(r"strm_[A-Za-z0-9_-]{16,128}\Z")
_RANGE = re.compile(r"bytes=(?:(\d+)-(\d*)|(\d*)-(\d+))\Z")
_ERROR_CODES = frozenset(
    {
        "playback_disabled",
        "contract_unverified",
        "manifest_out_of_scope",
        "timeout",
        "remote_failed",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class ManagedManifestId:
    """Opaque identifier owned by the managed STRM manifest, never a pickcode."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _MANIFEST_ID.fullmatch(self.value):
            raise PlaybackContractError("invalid_manifest_id")

    def __repr__(self) -> str:
        return "ManagedManifestId(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ByteRange:
    start: int | None
    end: int | None

    def __post_init__(self) -> None:
        if self.start is None and self.end is None:
            raise PlaybackContractError("invalid_range")
        if self.start is not None and self.start < 0:
            raise PlaybackContractError("invalid_range")
        if self.end is not None and self.end < 0:
            raise PlaybackContractError("invalid_range")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise PlaybackContractError("invalid_range")

    def __repr__(self) -> str:
        return "ByteRange(<redacted>)"


def parse_byte_range(value: str | None) -> ByteRange | None:
    """Accept exactly one RFC 7233 byte range; no ranges means no forwarding."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise PlaybackContractError("invalid_range")
    matched = _RANGE.fullmatch(value)
    if matched is None:
        raise PlaybackContractError("invalid_range")
    start = matched.group(1) or matched.group(3)
    end = matched.group(2) or matched.group(4)
    return ByteRange(int(start) if start else None, int(end) if end else None)


@dataclass(frozen=True, slots=True, repr=False)
class PlaybackRequest:
    manifest_id: ManagedManifestId
    method: PlaybackMethod
    byte_range: ByteRange | None = None

    def __repr__(self) -> str:
        return (
            f"PlaybackRequest(manifest_id=<redacted>, method={self.method.value!r}, "
            f"has_range={self.byte_range is not None!r})"
        )


def make_playback_request(
    manifest_id: str, method: str, range_header: str | None = None
) -> PlaybackRequest:
    """Build a validated request without retaining a raw HTTP header."""

    try:
        normalized_method = PlaybackMethod(method)
    except (TypeError, ValueError) as exc:
        raise PlaybackContractError("invalid_method") from exc
    return PlaybackRequest(
        ManagedManifestId(manifest_id),
        normalized_method,
        parse_byte_range(range_header),
    )


@dataclass(frozen=True, slots=True)
class PlaybackGate:
    """C05 remains disabled and unverified until an explicit real-contract review."""

    enabled: bool = False
    contract_verified: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class PlaybackGateDecision:
    allowed: bool
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            f"PlaybackGateDecision(allowed={self.allowed!r}, "
            f"error_code={self.error_code!r})"
        )


def evaluate_playback_gate(gate: PlaybackGate) -> PlaybackGateDecision:
    if not gate.enabled:
        return PlaybackGateDecision(False, "playback_disabled")
    if not gate.contract_verified:
        return PlaybackGateDecision(False, "contract_unverified")
    return PlaybackGateDecision(True)


@dataclass(frozen=True, slots=True, repr=False)
class ForwardingPolicy:
    upstream_method: PlaybackMethod
    forward_range: bool

    def __repr__(self) -> str:
        return (
            f"ForwardingPolicy(upstream_method={self.upstream_method.value!r}, "
            f"forward_range={self.forward_range!r})"
        )


def forwarding_policy(request: PlaybackRequest) -> ForwardingPolicy:
    """Freeze C05 forwarding: HEAD has no Range; GET forwards one valid Range."""

    return ForwardingPolicy(
        upstream_method=request.method,
        forward_range=request.method is PlaybackMethod.GET
        and request.byte_range is not None,
    )


@dataclass(frozen=True, slots=True, repr=False)
class DynamicLinkOutcome:
    """Transient dynamic-link result; ``url`` must never be persisted or logged."""

    status: PlaybackStatus
    error_code: str | None = None
    url: str | None = None
    policy: ForwardingPolicy | None = None
    request_headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.error_code is not None and self.error_code not in _ERROR_CODES:
            raise PlaybackContractError("invalid_error_code")
        if self.status is PlaybackStatus.READY:
            if not isinstance(self.url, str) or not self.url:
                raise PlaybackContractError("invalid_dynamic_link")
        elif self.url is not None:
            raise PlaybackContractError("invalid_dynamic_link")
        if any(
            not isinstance(name, str)
            or name.casefold() != "user-agent"
            or not isinstance(value, str)
            or not value
            or len(value) > 512
            for name, value in self.request_headers
        ):
            raise PlaybackContractError("invalid_request_headers")

    def __repr__(self) -> str:
        return (
            f"DynamicLinkOutcome(status={self.status.value!r}, "
            f"error_code={self.error_code!r}, has_url={self.url is not None!r}, "
            f"policy={self.policy!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PlaybackCall:
    method: PlaybackMethod
    has_range: bool

    def __repr__(self) -> str:
        return (
            f"PlaybackCall(method={self.method.value!r}, has_range={self.has_range!r})"
        )


class P115PlaybackGateway(Protocol):
    """Dynamic-link boundary for the managed STRM manifest."""

    async def resolve(
        self,
        request: PlaybackRequest,
        *,
        gate: PlaybackGate,
        allowed_library_ids: Collection[str] | None = None,
    ) -> DynamicLinkOutcome: ...


def classify_playback_exception(error: BaseException) -> DynamicLinkOutcome:
    """Convert unsafe transport failures to stable outcomes without their text."""

    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, TimeoutError):
        return DynamicLinkOutcome(PlaybackStatus.UNCERTAIN, "timeout")
    return DynamicLinkOutcome(PlaybackStatus.FAILED, "remote_failed")


class FakeP115PlaybackGateway:
    """Offline fake that accepts only explicitly managed manifest IDs in memory."""

    def __init__(
        self,
        outcomes: Mapping[str, DynamicLinkOutcome | BaseException] | None = None,
    ) -> None:
        self._outcomes = dict(outcomes or {})
        self.calls: list[PlaybackCall] = []

    def __repr__(self) -> str:
        return f"FakeP115PlaybackGateway(manifest_count={len(self._outcomes)})"

    async def resolve(
        self,
        request: PlaybackRequest,
        *,
        gate: PlaybackGate,
        allowed_library_ids: Collection[str] | None = None,
    ) -> DynamicLinkOutcome:
        del allowed_library_ids
        decision = evaluate_playback_gate(gate)
        if not decision.allowed:
            return DynamicLinkOutcome(
                PlaybackStatus.DISABLED
                if decision.error_code == "playback_disabled"
                else PlaybackStatus.UNVERIFIED,
                decision.error_code,
            )
        self.calls.append(PlaybackCall(request.method, request.byte_range is not None))
        outcome = self._outcomes.get(request.manifest_id.value)
        if outcome is None:
            return DynamicLinkOutcome(PlaybackStatus.NOT_FOUND, "manifest_out_of_scope")
        if isinstance(outcome, BaseException):
            return classify_playback_exception(outcome)
        policy = forwarding_policy(request)
        return DynamicLinkOutcome(
            outcome.status,
            outcome.error_code,
            outcome.url,
            policy,
            outcome.request_headers,
        )


__all__ = [
    "ByteRange",
    "DynamicLinkOutcome",
    "FakeP115PlaybackGateway",
    "ForwardingPolicy",
    "ManagedManifestId",
    "P115PlaybackGateway",
    "PlaybackCall",
    "PlaybackContractError",
    "PlaybackGate",
    "PlaybackGateDecision",
    "PlaybackMethod",
    "PlaybackRequest",
    "PlaybackStatus",
    "classify_playback_exception",
    "evaluate_playback_gate",
    "forwarding_policy",
    "make_playback_request",
    "parse_byte_range",
]
