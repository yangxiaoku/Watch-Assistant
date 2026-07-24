"""Pure episode coverage analysis for already identified video filenames."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    SINGLE_EPISODE = "single_episode"
    MULTI_SEASON = "multi_season"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EpisodeCoverage:
    status: CoverageStatus
    selected_season: int | None
    detected_seasons: tuple[int, ...]
    episodes_found: tuple[int, ...]
    expected_episode_count: int | None
    missing_episodes: tuple[int, ...]
    extra_episodes: tuple[int, ...]


_SEASON_MARKERS = (
    re.compile(r"(?<![a-z0-9])s\s*0*(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])seasons?\s*0*(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])0*(\d{1,3})(?!\d)\s*x", re.IGNORECASE),
    re.compile(r"第\s*0*(\d{1,3})(?!\d)\s*季"),
)
_SEASON_RANGES = (
    re.compile(
        r"(?<![a-z0-9])s\s*0*(\d{1,3})(?!\d)\s*[-~到至]\s*"
        r"s\s*0*(\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![a-z0-9])seasons?\s*0*(\d{1,3})(?!\d)\s*[-~到至]\s*"
        r"(?:seasons?\s*)?0*(\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"第\s*0*(\d{1,3})(?!\d)\s*季?\s*[-~到至]\s*"
        r"第?\s*0*(\d{1,3})(?!\d)\s*季"
    ),
)

_EPISODE_SEQUENCE = (
    r"0*\d{1,3}(?!\d)"
    r"(?:\s*[-~]\s*(?:e\s*)?0*\d+(?!\d)|"
    r"\s*e\s*0*\d{1,3}(?!\d))*"
)
_COMPACT_EPISODES = re.compile(
    r"(?<![a-z0-9])s\s*0*(?P<season>\d{1,3})(?!\d)\s*"
    rf"(?P<episodes>e\s*{_EPISODE_SEQUENCE})",
    re.IGNORECASE,
)
_X_EPISODES = re.compile(
    r"(?<![a-z0-9])0*(?P<season>\d{1,3})(?!\d)\s*x\s*"
    rf"(?P<episodes>{_EPISODE_SEQUENCE})",
    re.IGNORECASE,
)
_WORD_EPISODES = re.compile(
    r"(?<![a-z0-9])seasons?\s*0*(?P<season>\d{1,3})(?!\d)\s*"
    r"(?:episode|e)\s*"
    rf"(?P<episodes>{_EPISODE_SEQUENCE})",
    re.IGNORECASE,
)
_CHINESE_EPISODES = re.compile(
    r"第\s*0*(?P<season>\d{1,3})(?!\d)\s*季\s*"
    rf"第?\s*(?P<episodes>{_EPISODE_SEQUENCE})\s*集?"
)
_UNSEASONED_ENGLISH = re.compile(
    r"(?<![a-z0-9])(?:e|episode)\s*"
    rf"(?P<episodes>{_EPISODE_SEQUENCE})",
    re.IGNORECASE,
)
_UNSEASONED_CHINESE_RANGE = re.compile(
    r"(?<![a-z0-9])第\s*0*(\d{1,3})(?!\d)\s*集\s*[-~到至]\s*"
    r"第?\s*0*(\d{1,3})(?!\d)\s*集"
)
_UNSEASONED_CHINESE_SINGLE = re.compile(
    r"(?<![-~到至a-z0-9])第\s*0*(\d{1,3})(?!\d)\s*集"
)
_NUMBERED_STEM = re.compile(r"\d{1,3}")


def analyze_episode_coverage(
    file_names: Sequence[str],
    *,
    selected_season: int | None,
    expected_episode_count: int | None,
) -> EpisodeCoverage:
    """Analyze season and episode markers without inspecting external state."""
    _validate_nonnegative("selected_season", selected_season)
    _validate_nonnegative("expected_episode_count", expected_episode_count)

    detected_seasons: set[int] = set()
    records: list[tuple[int | None, int]] = []
    for file_name in file_names:
        basename = _normalize_basename(file_name)
        if not basename:
            continue
        detected_seasons.update(_detected_seasons(basename))
        seasoned, had_seasoned_syntax = _seasoned_episodes(basename)
        records.extend(seasoned)
        if not had_seasoned_syntax:
            records.extend(
                (None, episode) for episode in _unseasoned_episodes(basename)
            )
            stem = basename.rsplit(".", 1)[0]
            if _NUMBERED_STEM.fullmatch(stem.strip()):
                records.append((None, int(stem)))

    detected = tuple(sorted(detected_seasons))
    multi_season = len(detected) > 1 or (
        selected_season is not None
        and bool(detected)
        and any(season != selected_season for season in detected)
    )
    episodes = tuple(
        sorted(
            {
                episode
                for season, episode in records
                if selected_season is None
                or season is None
                or season == selected_season
            }
        )
    )
    missing, extra = _coverage_gaps(episodes, expected_episode_count)
    status = _status(
        episodes,
        expected_episode_count,
        missing,
        multi_season,
    )
    return EpisodeCoverage(
        status=status,
        selected_season=selected_season,
        detected_seasons=detected,
        episodes_found=episodes,
        expected_episode_count=expected_episode_count,
        missing_episodes=missing,
        extra_episodes=extra,
    )


def _normalize_basename(value: str) -> str:
    basename = re.split(r"[/\\]", value)[-1]
    return unicodedata.normalize("NFKC", basename).casefold()


def _detected_seasons(value: str) -> set[int]:
    seasons = {
        int(match.group(1))
        for pattern in _SEASON_MARKERS
        for match in pattern.finditer(value)
    }
    for pattern in _SEASON_RANGES:
        for match in pattern.finditer(value):
            seasons.update(int(item) for item in match.groups())
    return seasons


def _seasoned_episodes(value: str) -> tuple[list[tuple[int, int]], bool]:
    records: list[tuple[int, int]] = []
    had_syntax = False
    for pattern in (
        _COMPACT_EPISODES,
        _X_EPISODES,
        _WORD_EPISODES,
        _CHINESE_EPISODES,
    ):
        for match in pattern.finditer(value):
            had_syntax = True
            season = int(match.group("season"))
            episodes = _expand_episode_sequence(match.group("episodes"))
            records.extend((season, episode) for episode in episodes)
    return records, had_syntax


def _unseasoned_episodes(value: str) -> list[int]:
    episodes: list[int] = []
    for match in _UNSEASONED_ENGLISH.finditer(value):
        episodes.extend(_expand_episode_sequence(match.group("episodes")))
    for match in _UNSEASONED_CHINESE_RANGE.finditer(value):
        episodes.extend(_expand_range(int(match.group(1)), int(match.group(2))))
    for match in _UNSEASONED_CHINESE_SINGLE.finditer(value):
        episodes.append(int(match.group(1)))
    return episodes


def _expand_episode_sequence(value: str) -> list[int]:
    value = value.replace("第", "").replace("集", "")
    for range_match in re.finditer(r"[-~]\s*(?:e\s*)?0*(\d+)", value, re.IGNORECASE):
        if len(range_match.group(1).lstrip("0")) > 3:
            return []
    episodes: list[int] = []
    pattern = re.compile(
        r"(?<!\d)(?:e\s*)?0*(\d{1,3})(?!\d)"
        r"(?:\s*[-~]\s*(?:e\s*)?0*(\d{1,3})(?!\d))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(value):
        start = int(match.group(1))
        end = match.group(2)
        episodes.extend(_expand_range(start, int(end)) if end is not None else [start])
    return episodes


def _expand_range(start: int, end: int) -> list[int]:
    if end < start or end - start + 1 > 200:
        return []
    return list(range(start, end + 1))


def _coverage_gaps(
    episodes: tuple[int, ...], expected: int | None
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if expected is None or expected == 0:
        return (), ()
    expected_episodes = set(range(1, expected + 1))
    found = set(episodes)
    return (
        tuple(sorted(expected_episodes - found)),
        tuple(sorted(found - expected_episodes)),
    )


def _status(
    episodes: tuple[int, ...],
    expected: int | None,
    missing: tuple[int, ...],
    multi_season: bool,
) -> CoverageStatus:
    if multi_season:
        return CoverageStatus.MULTI_SEASON
    if expected is not None and expected > 0 and not missing:
        return CoverageStatus.COMPLETE
    if len(episodes) == 1:
        return CoverageStatus.SINGLE_EPISODE
    if expected is not None and expected > 0 and len(episodes) > 1:
        return CoverageStatus.PARTIAL
    if len(episodes) > 1:
        return CoverageStatus.UNKNOWN
    return CoverageStatus.UNKNOWN


def _validate_nonnegative(name: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative")
