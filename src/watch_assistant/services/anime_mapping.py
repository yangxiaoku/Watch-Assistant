"""Conservative, source-aware anime episode mapping primitives.

This module intentionally has no filesystem, network, or 115 dependency.  It
only turns an already identified episode reference into a reviewable mapping;
callers must provide the work identity and confirmed episode baseline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class AnimeSpecialKind(StrEnum):
    STANDARD = "standard"
    OVA = "ova"
    OAD = "oad"
    SP = "sp"
    RECAP = "recap"
    NCOP = "ncop"
    NCED = "nced"
    PV = "pv"
    UNKNOWN = "unknown"


class AnimeMappingStatus(StrEnum):
    MAPPED = "mapped"
    NEEDS_REVIEW = "needs_review"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AnimeSeasonRange:
    """A confirmed absolute-number range for one standard season."""

    season_number: int
    absolute_start: int
    episode_count: int
    source: str
    rule_version: str

    def __post_init__(self) -> None:
        if self.season_number <= 0:
            raise ValueError("season_number must be positive")
        if self.absolute_start <= 0:
            raise ValueError("absolute_start must be positive")
        if self.episode_count <= 0:
            raise ValueError("episode_count must be positive")
        _validate_text(self.source, "source", 64)
        _validate_text(self.rule_version, "rule_version", 64)

    @property
    def absolute_end(self) -> int:
        return self.absolute_start + self.episode_count - 1

    def contains(self, absolute_episode: int) -> bool:
        return self.absolute_start <= absolute_episode <= self.absolute_end

    def to_key(self, absolute_episode: int) -> AnimeEpisodeKey:
        if not self.contains(absolute_episode):
            raise ValueError("absolute episode is outside season range")
        return AnimeEpisodeKey(
            absolute_episode=absolute_episode,
            season_number=self.season_number,
            episode_number=absolute_episode - self.absolute_start + 1,
        )


@dataclass(frozen=True, slots=True)
class AnimeEpisodeKey:
    """A logical episode identity; unknown values remain None."""

    absolute_episode: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    special_kind: AnimeSpecialKind = AnimeSpecialKind.STANDARD

    def __post_init__(self) -> None:
        for name in ("absolute_episode", "season_number", "episode_number"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{name} must be positive when provided")
        if self.special_kind == AnimeSpecialKind.STANDARD and (
            self.season_number is None and self.episode_number is not None
            or self.season_number is not None and self.episode_number is None
        ):
            raise ValueError("standard episode needs both season and episode")
        if self.special_kind != AnimeSpecialKind.STANDARD and (
            self.season_number is not None or self.episode_number is not None
        ):
            raise ValueError("special content cannot be a standard season episode")


@dataclass(frozen=True, slots=True)
class AnimeFileReference:
    """Filename evidence before it is associated with a work identity."""

    original_filename: str
    keys: tuple[AnimeEpisodeKey, ...]
    special_kind: AnimeSpecialKind
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnimeMappingDecision:
    status: AnimeMappingStatus
    keys: tuple[AnimeEpisodeKey, ...] = ()
    source: str | None = None
    evidence: tuple[str, ...] = ()


_STANDARD_RE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<season>\d{1,3})[ ._-]*E(?P<start>\d{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>\d{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_ABSOLUTE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:EP|EPISODE|#)[ ._-]*0*(?P<start>\d{1,4})"
    r"(?:[ ._-]*(?:-|~|TO)[ ._-]*0*(?P<end>\d{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"^0*(?P<start>\d{1,4})(?:[-~]0*(?P<end>\d{1,4}))?$")
_SPECIAL_MARKERS = (
    (AnimeSpecialKind.NCOP, re.compile(r"(?<![A-Za-z0-9])NCOP(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.NCED, re.compile(r"(?<![A-Za-z0-9])NCED(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.RECAP, re.compile(r"(?<![A-Za-z0-9])RECAP(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.OVA, re.compile(r"(?<![A-Za-z0-9])OVA(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.OAD, re.compile(r"(?<![A-Za-z0-9])OAD(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.SP, re.compile(r"(?<![A-Za-z0-9])SP(?![A-Za-z0-9])", re.IGNORECASE)),
    (AnimeSpecialKind.PV, re.compile(r"(?<![A-Za-z0-9])PV(?![A-Za-z0-9])", re.IGNORECASE)),
)


def parse_anime_file_reference(filename: str) -> AnimeFileReference:
    """Extract standard/absolute episode evidence without making a match decision."""

    if not isinstance(filename, str):
        raise TypeError("filename must be a string")
    original = unicodedata.normalize("NFKC", filename).strip()
    basename = re.split(r"[/\\]", original)[-1]
    stem = basename.rsplit(".", 1)[0].replace(".", " ").replace("_", " ")
    special_matches = tuple(kind for kind, pattern in _SPECIAL_MARKERS if pattern.search(stem))
    if len(special_matches) > 1:
        return AnimeFileReference(basename, (), AnimeSpecialKind.UNKNOWN, ("special_conflict",))
    special = special_matches[0] if special_matches else AnimeSpecialKind.STANDARD

    if special != AnimeSpecialKind.STANDARD:
        return AnimeFileReference(basename, (), special, (f"special:{special.value}",))

    standard = _range_matches(_STANDARD_RE.search(stem), "season", "start", "end")
    if standard:
        return AnimeFileReference(
            basename,
            tuple(AnimeEpisodeKey(season_number=season, episode_number=episode) for season, episode in standard),
            special,
            ("standard_season_episode",),
        )
    absolute = _range_matches(_ABSOLUTE_RE.search(stem), None, "start", "end")
    if not absolute:
        bare = _BARE_NUMBER_RE.fullmatch(stem.strip())
        absolute = _range_matches(bare, None, "start", "end") if bare else ()
    if absolute:
        return AnimeFileReference(
            basename,
            tuple(AnimeEpisodeKey(absolute_episode=episode) for _, episode in absolute),
            special,
            ("absolute_episode",),
        )
    return AnimeFileReference(basename, (), special, ("episode_unknown",))


def resolve_absolute_episode(
    absolute_episode: int,
    season_ranges: tuple[AnimeSeasonRange, ...],
    *,
    explicit: dict[int, AnimeEpisodeKey] | None = None,
) -> AnimeMappingDecision:
    """Resolve one absolute episode, refusing overlapping or malformed baselines."""

    if isinstance(absolute_episode, bool) or absolute_episode <= 0:
        raise ValueError("absolute_episode must be positive")
    if explicit and absolute_episode in explicit:
        key = explicit[absolute_episode]
        if key.absolute_episode not in (None, absolute_episode):
            return AnimeMappingDecision(AnimeMappingStatus.CONFLICT, evidence=("explicit_absolute_conflict",))
        return AnimeMappingDecision(
            AnimeMappingStatus.MAPPED,
            (AnimeEpisodeKey(absolute_episode=absolute_episode, season_number=key.season_number, episode_number=key.episode_number),),
            "explicit",
            ("explicit_episode_mapping",),
        )
    matches = tuple(item for item in season_ranges if item.contains(absolute_episode))
    if len(matches) > 1:
        return AnimeMappingDecision(AnimeMappingStatus.CONFLICT, evidence=("season_range_overlap",))
    if not matches:
        return AnimeMappingDecision(AnimeMappingStatus.NEEDS_REVIEW, evidence=("absolute_episode_out_of_range",))
    season = matches[0]
    return AnimeMappingDecision(
        AnimeMappingStatus.MAPPED,
        (season.to_key(absolute_episode),),
        season.source,
        ("confirmed_season_offset", f"rule:{season.rule_version}"),
    )


def _range_matches(match: re.Match[str] | None, season_name: str | None, start_name: str, end_name: str) -> tuple[tuple[int | None, int], ...]:
    if match is None:
        return ()
    start = int(match.group(start_name))
    end = int(match.group(end_name)) if match.group(end_name) else start
    if end < start or end - start >= 200:
        return ()
    season = int(match.group(season_name)) if season_name else None
    return tuple((season, value) for value in range(start, end + 1))


def _validate_text(value: str, name: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"invalid {name}")
