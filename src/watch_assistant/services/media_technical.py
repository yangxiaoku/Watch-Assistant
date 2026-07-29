"""Evidence-only media technical inspection result modeling.

The module deliberately accepts an already captured observation.  Obtaining a
115 direct link, reading media bytes, and invoking a probe tool belong to a
later guarded adapter and are not performed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from watch_assistant.services.media_parser import MediaParseResult


class InspectionDepth(StrEnum):
    L1 = "l1_metadata"
    L2 = "l2_sampled"
    L3 = "l3_full"


class TechnicalStatus(StrEnum):
    METADATA_VERIFIED = "metadata_verified"
    SAMPLED_OK = "sampled_ok"
    SUSPECT = "suspect"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TechnicalReason(StrEnum):
    DIRECT_LINK_UNAVAILABLE = "direct_link_unavailable"
    RANGE_NOT_SUPPORTED = "range_not_supported"
    CONTAINER_UNRECOGNIZED = "container_unrecognized"
    HEADER_PARSE_FAILED = "header_parse_failed"
    NO_VIDEO_TRACK = "no_video_track"
    DURATION_INVALID = "duration_invalid"
    SEEK_FAILED = "seek_failed"
    SAMPLE_DECODE_FAILED = "sample_decode_failed"
    DECLARED_METADATA_MISMATCH = "declared_metadata_mismatch"
    PROBE_TIMEOUT = "probe_timeout"
    READ_BUDGET_EXCEEDED = "read_budget_exceeded"
    TOOL_INCOMPATIBLE = "tool_incompatible"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class DeclaredTechnicalTags:
    """Technical labels declared by a filename or resource description."""

    resolution: str | None = None
    video_codec: str | None = None
    hdr: str | None = None
    atmos: bool | None = None


@dataclass(frozen=True, slots=True)
class MeasuredTechnicalObservation:
    """Sanitized probe output; it contains no URL, path, or media bytes."""

    container: str | None = None
    duration_seconds: float | None = None
    video_track_count: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_codec: str | None = None
    hdr: str | None = None
    atmos: bool | None = None
    read_bytes: int = 0
    sample_points: int = 0
    sample_successes: int = 0
    probe_error: TechnicalReason | None = None
    tool_version: str | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds < 0
        ):
            raise ValueError("duration_seconds must be finite and non-negative")
        for name in ("video_track_count", "video_width", "video_height", "read_bytes", "sample_points", "sample_successes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be non-negative")
        if self.sample_successes > self.sample_points:
            raise ValueError("sample_successes cannot exceed sample_points")
        if self.tool_version is not None and (not self.tool_version.strip() or len(self.tool_version) > 64):
            raise ValueError("invalid tool_version")


@dataclass(frozen=True, slots=True)
class TechnicalInspectionReport:
    depth: InspectionDepth
    status: TechnicalStatus
    reasons: tuple[TechnicalReason, ...]
    mismatches: tuple[str, ...]
    measured: MeasuredTechnicalObservation


def declared_tags_from_parse(parsed: MediaParseResult) -> DeclaredTechnicalTags:
    """Keep filename declarations separate from measured technical fields."""

    return DeclaredTechnicalTags(
        resolution=parsed.resolution,
        video_codec=parsed.video_codec,
        hdr=parsed.hdr,
        atmos=parsed.atmos,
    )


def inspect_observation(
    observation: MeasuredTechnicalObservation,
    *,
    depth: InspectionDepth,
    declared: DeclaredTechnicalTags | None = None,
) -> TechnicalInspectionReport:
    """Convert a sanitized observation into a conservative public report."""

    reasons: list[TechnicalReason] = []
    mismatches: list[str] = []
    if observation.probe_error is not None:
        reasons.append(observation.probe_error)
        return TechnicalInspectionReport(depth, TechnicalStatus.FAILED, tuple(reasons), (), observation)

    if observation.container is None:
        reasons.append(TechnicalReason.INSUFFICIENT_DATA)
    if observation.video_track_count == 0:
        reasons.append(TechnicalReason.NO_VIDEO_TRACK)
    if observation.duration_seconds == 0:
        reasons.append(TechnicalReason.DURATION_INVALID)

    if declared is not None:
        mismatches.extend(_metadata_mismatches(declared, observation))
        if mismatches:
            reasons.append(TechnicalReason.DECLARED_METADATA_MISMATCH)

    if depth == InspectionDepth.L2:
        if observation.sample_points == 0:
            reasons.append(TechnicalReason.INSUFFICIENT_DATA)
        elif observation.sample_successes < observation.sample_points:
            reasons.append(TechnicalReason.SAMPLE_DECODE_FAILED)
    status = _status_for(depth, observation, reasons)
    return TechnicalInspectionReport(
        depth,
        status,
        tuple(dict.fromkeys(reasons)),
        tuple(dict.fromkeys(mismatches)),
        observation,
    )


def _status_for(
    depth: InspectionDepth,
    observation: MeasuredTechnicalObservation,
    reasons: list[TechnicalReason],
) -> TechnicalStatus:
    if depth == InspectionDepth.L2:
        if observation.sample_points == 0:
            return TechnicalStatus.UNKNOWN
        if observation.sample_successes < observation.sample_points:
            return TechnicalStatus.SUSPECT
        if TechnicalReason.DECLARED_METADATA_MISMATCH in reasons:
            return TechnicalStatus.SUSPECT
        if any(reason not in {TechnicalReason.DECLARED_METADATA_MISMATCH} for reason in reasons):
            return TechnicalStatus.SUSPECT
        return TechnicalStatus.SAMPLED_OK
    if any(reason in {TechnicalReason.NO_VIDEO_TRACK, TechnicalReason.DURATION_INVALID} for reason in reasons):
        return TechnicalStatus.SUSPECT
    if TechnicalReason.DECLARED_METADATA_MISMATCH in reasons:
        return TechnicalStatus.SUSPECT
    if observation.container is None or observation.duration_seconds is None or observation.video_track_count is None:
        return TechnicalStatus.UNKNOWN
    return TechnicalStatus.METADATA_VERIFIED


def _metadata_mismatches(
    declared: DeclaredTechnicalTags,
    measured: MeasuredTechnicalObservation,
) -> tuple[str, ...]:
    differences: list[str] = []
    actual_resolution = _resolution_label(measured.video_height)
    if declared.resolution and actual_resolution and _resolution_label_from_text(declared.resolution) != actual_resolution:
        differences.append("resolution")
    if declared.video_codec and measured.video_codec and _normalize(declared.video_codec) != _normalize(measured.video_codec):
        differences.append("video_codec")
    if declared.hdr and measured.hdr and _normalize(declared.hdr) != _normalize(measured.hdr):
        differences.append("hdr")
    if declared.atmos is True and measured.atmos is False:
        differences.append("atmos")
    return tuple(differences)


def _resolution_label(height: int | None) -> str | None:
    if height is None:
        return None
    if height >= 2000:
        return "2160p"
    if height >= 1000:
        return "1080p"
    if height >= 600:
        return "720p"
    return "sd"


def _resolution_label_from_text(value: str) -> str:
    normalized = _normalize(value)
    if normalized in {"4k", "uhd", "2160p"}:
        return "2160p"
    if normalized == "1080p":
        return "1080p"
    if normalized == "720p":
        return "720p"
    return normalized


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())
