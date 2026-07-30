"""Pure, conservative version policy for organization previews.

The policy only compares evidence already present in a verified scan.  It
never treats missing technical metadata as inferior and never performs a
remote write.  Replacement remains a separate transport capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from watch_assistant.services.media_parser import MediaParseResult

PolicyOutcome = Literal["candidate", "existing", "coexist", "review"]


@dataclass(frozen=True, slots=True)
class OrganizationConflictPolicy:
    """Versioned settings used to compare two logical media versions."""

    prefer_remux: bool = True
    prefer_resolution: bool = True
    prefer_dolby: bool = False
    conflict_mode: int = 2
    multi_version_enabled: bool = False
    media_probe_enabled: bool = False
    ai_identification_enabled: bool = False
    cleanup_empty_directories: bool = False
    strm_linkage_enabled: bool = False
    version: str = "organization-policy-v1"

    def __post_init__(self) -> None:
        for name in (
            "prefer_remux",
            "prefer_resolution",
            "prefer_dolby",
            "multi_version_enabled",
            "media_probe_enabled",
            "ai_identification_enabled",
            "cleanup_empty_directories",
            "strm_linkage_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"invalid_{name}")
        if self.conflict_mode not in (0, 1, 2):
            raise ValueError("invalid_conflict_mode")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("invalid_policy_version")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "prefer_remux": self.prefer_remux,
            "prefer_resolution": self.prefer_resolution,
            "prefer_dolby": self.prefer_dolby,
            "conflict_mode": self.conflict_mode,
            "multi_version_enabled": self.multi_version_enabled,
            "media_probe_enabled": self.media_probe_enabled,
            "ai_identification_enabled": self.ai_identification_enabled,
            "cleanup_empty_directories": self.cleanup_empty_directories,
            "strm_linkage_enabled": self.strm_linkage_enabled,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> OrganizationConflictPolicy:
        if value is None:
            return cls()
        try:
            return cls(
                prefer_remux=value.get("prefer_remux", True),
                prefer_resolution=value.get("prefer_resolution", True),
                prefer_dolby=value.get("prefer_dolby", False),
                conflict_mode=value.get("conflict_mode", 2),
                multi_version_enabled=value.get("multi_version_enabled", False),
                media_probe_enabled=value.get("media_probe_enabled", False),
                ai_identification_enabled=value.get("ai_identification_enabled", False),
                cleanup_empty_directories=value.get("cleanup_empty_directories", False),
                strm_linkage_enabled=value.get("strm_linkage_enabled", False),
                version=value.get("version", "organization-policy-v1"),
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid_organization_policy") from error


@dataclass(frozen=True, slots=True)
class VersionEvidence:
    """Sanitized technical evidence used by the policy."""

    remux: bool | None = None
    resolution_rank: int | None = None
    dolby: bool | None = None
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "remux": self.remux,
            "resolution_rank": self.resolution_rank,
            "dolby": self.dolby,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class VersionDecision:
    outcome: PolicyOutcome
    reason: str
    dimension: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "dimension": self.dimension,
        }


def evidence_from_parse(
    parsed: MediaParseResult, *, size_bytes: int | None = None
) -> VersionEvidence:
    """Extract only bounded filename evidence; unknown stays unknown."""

    if not isinstance(parsed, MediaParseResult):
        raise TypeError("invalid_media_parse")
    source = parsed.source.casefold() if isinstance(parsed.source, str) else ""
    remux: bool | None = None
    if source:
        if source in {"remux", "bluray", "bdrip", "brrip"}:
            remux = True
        elif source in {"web-dl", "webrip", "hdtv"}:
            remux = False
    resolution_rank = _resolution_rank(parsed.resolution)
    dolby: bool | None = None
    if parsed.dolby_vision is True or parsed.dolby_audio is True:
        dolby = True
    elif parsed.hdr is not None or parsed.dolby_vision is False or parsed.dolby_audio is False:
        dolby = False
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
    ):
        raise ValueError("invalid_size_bytes")
    return VersionEvidence(remux, resolution_rank, dolby, size_bytes)


def compare_versions(
    candidate: VersionEvidence,
    existing: VersionEvidence,
    policy: OrganizationConflictPolicy,
) -> VersionDecision:
    """Return a deterministic decision without guessing unknown fields."""

    if not isinstance(candidate, VersionEvidence) or not isinstance(existing, VersionEvidence):
        raise TypeError("invalid_version_evidence")
    if not isinstance(policy, OrganizationConflictPolicy):
        raise TypeError("invalid_organization_policy")
    if policy.conflict_mode == 2:
        return VersionDecision("existing", "same_name_only")
    if (
        policy.multi_version_enabled
        and candidate.dolby is not None
        and existing.dolby is not None
        and candidate.dolby != existing.dolby
    ):
        return VersionDecision("coexist", "dolby_multi_version", "dolby")

    dimensions = (
        ("remux", candidate.remux, existing.remux, policy.prefer_remux),
        ("resolution", candidate.resolution_rank, existing.resolution_rank, policy.prefer_resolution),
        ("dolby", candidate.dolby, existing.dolby, policy.prefer_dolby),
    )
    for name, candidate_value, existing_value, prefer_high in dimensions:
        if candidate_value is None or existing_value is None or candidate_value == existing_value:
            continue
        candidate_wins = candidate_value > existing_value if prefer_high else candidate_value < existing_value
        return VersionDecision(
            "candidate" if candidate_wins else "existing",
            f"{name}_priority",
            name,
        )
    if (
        candidate.size_bytes is not None
        and existing.size_bytes is not None
        and candidate.size_bytes != existing.size_bytes
    ):
        candidate_wins = (
            candidate.size_bytes > existing.size_bytes
            if policy.conflict_mode == 0
            else candidate.size_bytes < existing.size_bytes
        )
        return VersionDecision(
            "candidate" if candidate_wins else "existing",
            "size_priority",
            "size",
        )
    return VersionDecision("review", "version_evidence_tie")


def _resolution_rank(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold().replace(" ", "")
    if normalized in {"4k", "uhd", "2160p"}:
        return 2160
    if normalized == "1080p":
        return 1080
    if normalized == "720p":
        return 720
    if normalized == "sd":
        return 480
    return None


__all__ = [
    "OrganizationConflictPolicy",
    "VersionDecision",
    "VersionEvidence",
    "compare_versions",
    "evidence_from_parse",
]
