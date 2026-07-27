"""Match normalized resources to TMDB media metadata."""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from watch_assistant.schemas import MediaType, MovieMetadata, NormalizedResource

YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
EPISODE_PATTERN = re.compile(
    r"(?<![a-z0-9])s\d{1,2}(?:e\d{1,3})?(?!\d)|"
    r"(?<![a-z0-9])e\d{1,3}(?!\d)|"
    r"(?<![a-z0-9])season\s*\d+(?!\d)|"
    r"(?<![a-z0-9])episode\s*\d+(?!\d)|"
    r"全集|全季|全剧|全系列|complete\s+(?:series|collection|season)|"
    r"\u7b2c\s*\d+\s*[\u5b63\u96c6]|\u66f4\u65b0\u81f3\s*\d*\s*\u96c6?|\u5168\s*\d+\s*\u96c6",
    re.IGNORECASE,
)
MEDIA_PATTERN = re.compile(
    r"\b(?:2160p|1080p|720p|4k|blu[ .-]?ray|web[ .-]?dl|webrip|"
    r"remux|brrip|dvd[ .-]?rip|hdr|x26[45]|h[ .]?26[45])\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Match:
    resource: NormalizedResource
    rank_score: int


def validate_and_rank_resources(
    media: MovieMetadata,
    resources: list[NormalizedResource],
    *,
    alternative_titles: tuple[str, ...] = (),
    source_penalties: Mapping[str, int] | None = None,
    season_number: int | None = None,
) -> tuple[list[NormalizedResource], int]:
    matches: list[_Match] = []
    rejected = 0
    penalties = source_penalties or {}
    for resource in resources:
        score = _match_score(
            media,
            resource.name,
            alternative_titles=alternative_titles,
            season_number=season_number,
        )
        if score is None:
            rejected += 1
            continue
        relevance_score = _relevance_score(
            media,
            resource.name,
            alternative_titles=alternative_titles,
            season_number=season_number,
        )
        completeness_score = _completeness_score(resource.name, resource)
        rank_score = max(
            0,
            min(
                100,
                round(relevance_score * 0.65 + completeness_score * 0.35)
                - penalties.get(resource.source, 0),
            ),
        )
        resource.metadata.update(
            {
                "rank_score": rank_score,
                "relevance_score": relevance_score,
                "completeness_score": completeness_score,
            }
        )
        matches.append(_Match(resource, rank_score))

    matches.sort(
        key=lambda item: (
            -item.rank_score,
            -int(item.resource.seeders if item.resource.seeders is not None else -1),
            -int(
                item.resource.size_bytes if item.resource.size_bytes is not None else -1
            ),
            -item.resource.captured_at.timestamp(),
            item.resource.canonical_key,
        )
    )
    return [match.resource for match in matches], rejected


def resource_matches_media(
    media: MovieMetadata,
    resource_name: str,
    *,
    alternative_titles: tuple[str, ...] = (),
    season_number: int | None = None,
) -> bool:
    return (
        _match_score(
            media,
            resource_name,
            alternative_titles=alternative_titles,
            season_number=season_number,
        )
        is not None
    )


def _match_score(
    media: MovieMetadata,
    resource_name: str,
    *,
    alternative_titles: tuple[str, ...] = (),
    season_number: int | None = None,
) -> int | None:
    normalized_name = _normalize(resource_name)
    compact_name = normalized_name.replace(" ", "")
    has_episode_marker = (
        EPISODE_PATTERN.search(unicodedata.normalize("NFKC", resource_name)) is not None
    )
    if media.media_type == MediaType.MOVIE and has_episode_marker:
        return None

    season_numbers = _season_numbers(resource_name)
    if (
        media.media_type == MediaType.TV
        and season_number is not None
        and season_numbers != {season_number}
    ):
        return None

    title_match = _alias_matches(media.title, normalized_name, compact_name)
    original_match = bool(media.original_title) and _alias_matches(
        media.original_title or "", normalized_name, compact_name
    )
    alternative_matches = [
        alias
        for alias in alternative_titles
        if _alias_matches(alias, normalized_name, compact_name)
    ]
    if not title_match and not original_match and not alternative_matches:
        return None

    years = {int(value) for value in YEAR_PATTERN.findall(normalized_name)}
    year_score = 0
    has_matching_year = False
    target_year = _target_year(media, season_number)
    if target_year is not None and years:
        closest = min(abs(year - target_year) for year in years)
        if closest > 1:
            return None
        if closest <= 1:
            has_matching_year = True
            year_score = 30 if closest == 0 else 20

    has_media_marker = MEDIA_PATTERN.search(normalized_name) is not None
    matched_aliases = [
        alias
        for alias, matched in (
            (media.title, title_match),
            (media.original_title or "", original_match),
        )
        if matched
    ]
    matched_aliases.extend(alternative_matches)
    if (
        all(_is_ambiguous_alias(alias) for alias in matched_aliases)
        and not has_matching_year
        and not has_media_marker
        and not (media.media_type == MediaType.TV and has_episode_marker)
    ):
        return None

    score = (100 if title_match else 90 if original_match else 85) + year_score
    if re.search(r"\b(?:2160p|4k)\b", normalized_name):
        score += 20
    elif re.search(r"\b1080p\b", normalized_name):
        score += 15
    if re.search(r"\b(?:remux|blu[ .-]?ray)\b", normalized_name):
        score += 10
    elif re.search(r"\bweb[ .-]?dl\b", normalized_name):
        score += 8
    return score


def _relevance_score(
    media: MovieMetadata,
    resource_name: str,
    *,
    alternative_titles: tuple[str, ...],
    season_number: int | None,
) -> int:
    normalized_name = _normalize(resource_name)
    compact_name = normalized_name.replace(" ", "")
    title_match = _alias_matches(media.title, normalized_name, compact_name)
    original_match = bool(media.original_title) and _alias_matches(
        media.original_title or "", normalized_name, compact_name
    )
    alternative_match = any(
        _alias_matches(alias, normalized_name, compact_name)
        for alias in alternative_titles
    )
    title_score = (
        45 if title_match else 40 if original_match else 35 if alternative_match else 0
    )
    years = {int(value) for value in YEAR_PATTERN.findall(normalized_name)}
    target_year = _target_year(media, season_number)
    year_score = 0
    if target_year is not None and years:
        closest = min(abs(year - target_year) for year in years)
        year_score = 35 if closest == 0 else 25 if closest == 1 else 0
    season_score = (
        20
        if media.media_type == MediaType.TV
        and season_number is not None
        and _season_numbers(resource_name) == {season_number}
        else 0
    )
    return max(0, min(100, title_score + year_score + season_score))


def _completeness_score(name: str, resource: NormalizedResource) -> int:
    normalized_name = _normalize(name)
    if re.search(r"\b(?:2160p|4k)\b", normalized_name):
        resolution = 35
    elif re.search(r"\b1080p\b", normalized_name):
        resolution = 30
    elif re.search(r"\b720p\b", normalized_name):
        resolution = 20
    else:
        resolution = 10
    if re.search(r"\b(?:remux|blu[ .-]?ray)\b", normalized_name):
        source = 25
    elif re.search(r"\b(?:web[ .-]?dl|webrip)\b", normalized_name):
        source = 20
    else:
        source = 10
    if resource.seeders is None:
        seeders = 0
    elif resource.seeders <= 0:
        seeders = 1
    elif resource.seeders <= 5:
        seeders = 8
    elif resource.seeders <= 20:
        seeders = 15
    elif resource.seeders <= 100:
        seeders = 22
    else:
        seeders = 25
    size = 15 if resource.size_bytes is not None and resource.size_bytes > 0 else 0
    return max(0, min(100, resolution + source + seeders + size))


def _target_year(media: MovieMetadata, season_number: int | None) -> int | None:
    if media.media_type == MediaType.TV:
        if season_number is None:
            return None
        for season in media.seasons:
            if season.season_number != season_number:
                continue
            air_date = season.air_date
            if not isinstance(air_date, str):
                return None
            try:
                return date.fromisoformat(air_date).year
            except ValueError:
                return None
        return None
    if media.media_type == MediaType.MOVIE:
        return media.release_year
    return None


def _alias_matches(alias: str, normalized_name: str, compact_name: str) -> bool:
    normalized_alias = _normalize(alias)
    if not normalized_alias:
        return False
    if _contains_cjk(normalized_alias):
        return normalized_alias.replace(" ", "") in compact_name
    return f" {normalized_alias} " in f" {normalized_name} "


def _is_ambiguous_alias(alias: str) -> bool:
    normalized = _normalize(alias)
    if _contains_cjk(normalized):
        return len(normalized.replace(" ", "")) <= 3
    return len(normalized.split()) <= 1


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _season_numbers(value: str) -> set[int]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    numbers: set[int] = set()
    for match in re.finditer(
        r"(?<![a-z0-9])s(?:eason)?s?\s*0*(\d{1,3})(?!\d)|"
        r"第\s*0*(\d{1,3})(?!\d)\s*季",
        normalized,
    ):
        value = match.group(1) or match.group(2)
        if value is not None:
            numbers.add(int(value))
    for match in re.finditer(
        r"(?<![a-z0-9])s(?:eason)?s?\s*0*(\d{1,3})(?!\d)\s*[-~到至]\s*"
        r"(?:s(?:eason)?s?\s*)?0*(\d{1,3})(?!\d)|"
        r"第\s*0*(\d{1,3})(?!\d)\s*季?\s*[-~到至]\s*"
        r"第?\s*0*(\d{1,3})(?!\d)\s*季",
        normalized,
    ):
        numbers.update(int(item) for item in match.groups() if item is not None)
    return numbers
