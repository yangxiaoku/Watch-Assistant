"""Offline TMDB matching domain primitives.

The matcher deliberately stops at a decision. It does not create an
organization plan and never performs a remote write.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from watch_assistant.schemas import MediaType
from watch_assistant.services.media_parser import MediaParseResult


class MatchStatus(StrEnum):
    ACCEPTED = "accepted"
    AUTO_ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    NO_CANDIDATES = "no_candidates"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    UNAVAILABLE = "unavailable"


class MatchConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MatchSource(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    NONE = "none"


class MediaKind(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    ANIME = "anime"
    DOCUMENTARY = "documentary"
    VARIETY = "variety"
    UNKNOWN = "unknown"


class SpecialKind(StrEnum):
    STANDARD = "standard"
    SP = "sp"
    OVA = "ova"
    OAD = "oad"
    ONA = "ona"
    UNKNOWN = "unknown"


class MatchReason(StrEnum):
    TITLE_MISSING = "title_missing"
    TITLE_MISMATCH = "title_mismatch"
    YEAR_CONFLICT = "year_conflict"
    YEAR_UNKNOWN = "year_unknown"
    MEDIA_TYPE_CONFLICT = "media_type_conflict"
    MEDIA_TYPE_UNKNOWN = "media_type_unknown"
    KIND_CONFLICT = "kind_conflict"
    KIND_UNKNOWN = "kind_unknown"
    ORIGIN_COUNTRY_CONFLICT = "origin_country_conflict"
    ORIGIN_COUNTRY_UNKNOWN = "origin_country_unknown"
    SEASON_OUT_OF_RANGE = "season_out_of_range"
    SEASON_UNKNOWN = "season_unknown"
    EPISODE_OUT_OF_RANGE = "episode_out_of_range"
    EPISODE_RANGE_NOT_COVERED = "episode_range_not_covered"
    EPISODE_UNKNOWN = "episode_unknown"
    SPECIAL_CONFLICT = "special_conflict"
    SPECIAL_BOUNDARY_UNKNOWN = "special_boundary_unknown"
    EMPTY_CANDIDATES = "empty_candidates"
    CONFLICTING_CANDIDATES = "conflicting_candidates"
    LOW_SCORE = "low_score"
    SCORE_MARGIN_INSUFFICIENT = "score_margin_insufficient"
    MANUAL_LOCK = "manual_lock"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    UNAVAILABLE = "unavailable"


class TmdbMatchError(RuntimeError):
    """Base class for stable, non-sensitive client failures."""

    def __init__(self, *_args: object) -> None:
        super().__init__()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def __str__(self) -> str:
        return type(self).__name__


class TmdbRateLimitError(TmdbMatchError):
    pass


class TmdbTimeoutError(TmdbMatchError):
    pass


class TmdbMalformedResponseError(TmdbMatchError):
    pass


class TmdbUnavailableError(TmdbMatchError):
    pass


@dataclass(frozen=True, slots=True)
class MediaMatchInput:
    """Safe query data derived from a parser result, never the source name."""

    title: str | None
    year: int | None = None
    year_candidates: tuple[int, ...] = ()
    media_type_hint: str = "unknown"
    kind: MediaKind = MediaKind.UNKNOWN
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    special_hints: tuple[str, ...] = ()
    origin_country_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _clean_public_text(self.title) or None)
        years = tuple(
            sorted(
                {
                    year
                    for year in self.year_candidates
                    if isinstance(year, int) and not isinstance(year, bool)
                }
            )
        )
        object.__setattr__(self, "year_candidates", years)
        object.__setattr__(self, "kind", _coerce_kind(self.kind))
        object.__setattr__(
            self, "special_hints", _canonical_special_hints(self.special_hints)
        )
        object.__setattr__(
            self,
            "origin_country_hints",
            _canonical_countries(self.origin_country_hints),
        )

    def __repr__(self) -> str:
        return "MediaMatchInput(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class TmdbSeason:
    season_number: int
    episode_count: int | None = None
    episode_numbers: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.season_number < 0:
            raise ValueError("invalid season")
        if self.episode_count is not None and self.episode_count < 0:
            raise ValueError("invalid episode count")
        numbers = tuple(sorted(set(self.episode_numbers)))
        if any(number < 0 for number in numbers):
            raise ValueError("invalid episode number")
        object.__setattr__(self, "episode_numbers", numbers)


@dataclass(frozen=True, slots=True)
class TmdbCandidate:
    tmdb_id: int
    media_type: MediaType
    title: str
    original_title: str | None = None
    # English-language title used to match English filenames (common in
    # torrents) against media that is stored under a localized title.
    english_title: str | None = None
    aliases: tuple[str, ...] = ()
    release_year: int | None = None
    origin_countries: tuple[str, ...] = ()
    seasons: tuple[TmdbSeason, ...] = ()
    kind: MediaKind = MediaKind.UNKNOWN
    special_kind: SpecialKind = SpecialKind.STANDARD

    def __post_init__(self) -> None:
        try:
            media_type = MediaType(self.media_type)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid candidate identity") from error
        if self.tmdb_id <= 0:
            raise ValueError("invalid candidate identity")
        object.__setattr__(self, "media_type", media_type)
        title = _clean_public_text(self.title)
        if not title:
            raise ValueError("candidate title is required")
        object.__setattr__(self, "title", title)
        if self.original_title is not None:
            object.__setattr__(
                self, "original_title", _clean_public_text(self.original_title) or None
            )
        if self.english_title is not None:
            object.__setattr__(
                self, "english_title", _clean_public_text(self.english_title) or None
            )
        aliases: list[str] = []
        seen: set[str] = set()
        for alias in self.aliases:
            clean = _clean_public_text(alias)
            key = _normalize(clean)
            if clean and key and key not in seen:
                seen.add(key)
                aliases.append(clean)
        aliases.sort(key=lambda value: (_normalize(value), value))
        object.__setattr__(self, "aliases", tuple(aliases[:20]))
        countries = tuple(
            sorted(
                {
                    country.strip().upper()
                    for country in self.origin_countries
                    if isinstance(country, str) and country.strip()
                }
            )
        )
        object.__setattr__(self, "origin_countries", countries)
        object.__setattr__(self, "seasons", _dedupe_seasons(self.seasons))
        object.__setattr__(self, "kind", _coerce_kind(self.kind))
        object.__setattr__(self, "special_kind", _coerce_special(self.special_kind))

    @property
    def identity(self) -> tuple[int, MediaType]:
        return self.tmdb_id, self.media_type

    def __repr__(self) -> str:
        return f"TmdbCandidate(tmdb_id={self.tmdb_id}, media_type={self.media_type.value!r})"

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TmdbCandidate:
        """Build a redacted candidate from a bounded TMDB-shaped payload."""

        if not isinstance(payload, Mapping):
            raise TmdbMalformedResponseError
        tmdb_id = payload.get("id", payload.get("tmdb_id"))
        media_type = payload.get("media_type", payload.get("type"))
        title = payload.get("title", payload.get("name"))
        if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool):
            raise TmdbMalformedResponseError
        try:
            parsed_type = MediaType(media_type)
        except (TypeError, ValueError) as error:
            raise TmdbMalformedResponseError from error
        if not isinstance(title, str) or not title.strip():
            raise TmdbMalformedResponseError
        aliases = payload.get("aliases", payload.get("alternative_titles", ()))
        if isinstance(aliases, Mapping):
            aliases = aliases.get("results", aliases.get("titles", ()))
        if not isinstance(aliases, (list, tuple)):
            raise TmdbMalformedResponseError
        alias_values: list[str] = []
        for item in aliases:
            if isinstance(item, str):
                alias_values.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("title"), str):
                alias_values.append(item["title"])
        countries = payload.get("origin_country", payload.get("origin_countries", ()))
        if (
            isinstance(countries, list)
            and countries
            and isinstance(countries[0], Mapping)
        ):
            countries = [item.get("iso_3166_1") for item in countries]
        if not isinstance(countries, (list, tuple)):
            raise TmdbMalformedResponseError
        return cls(
            tmdb_id=tmdb_id,
            media_type=parsed_type,
            title=title,
            original_title=payload.get("original_title", payload.get("original_name"))
            if isinstance(
                payload.get("original_title", payload.get("original_name")), str
            )
            else None,
            english_title=payload.get("english_title")
            if isinstance(payload.get("english_title"), str)
            else None,
            aliases=tuple(alias_values),
            release_year=_parse_year(
                payload.get(
                    "release_year",
                    payload.get("release_date", payload.get("first_air_date")),
                )
            ),
            origin_countries=tuple(item for item in countries if isinstance(item, str)),
            seasons=_parse_seasons(payload.get("seasons")),
            kind=_coerce_kind(payload.get("kind", MediaKind.UNKNOWN)),
            special_kind=_coerce_special(
                payload.get("special_kind", SpecialKind.UNKNOWN)
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    score: int
    title_match: str | None = None
    year_match: str | None = None
    media_type_match: bool | None = None
    kind_match: bool | None = None
    origin_country_match: bool | None = None
    season_match: bool | None = None
    episode_match: bool | None = None
    special_match: bool | None = None
    reasons: tuple[MatchReason, ...] = ()

    def __repr__(self) -> str:
        return f"MatchEvidence(score={self.score}, reasons={tuple(self.reasons)!r})"


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: TmdbCandidate
    score: int
    evidence: MatchEvidence
    eligible: bool

    def __repr__(self) -> str:
        return f"RankedCandidate(tmdb_id={self.candidate.tmdb_id}, score={self.score})"


@dataclass(frozen=True, slots=True)
class MatchDecision:
    status: MatchStatus
    selected: TmdbCandidate | None = None
    confidence: MatchConfidence | None = None
    score: int | None = None
    margin: int | None = None
    ranked_candidates: tuple[RankedCandidate, ...] = ()
    reasons: tuple[MatchReason, ...] = ()
    source: MatchSource = MatchSource.NONE

    @property
    def accepted(self) -> bool:
        return self.status == MatchStatus.ACCEPTED and self.selected is not None

    def __repr__(self) -> str:
        selected = self.selected.tmdb_id if self.selected is not None else None
        return f"MatchDecision(status={self.status.value!r}, selected={selected!r})"


def confirm_selected_candidate(decision: MatchDecision) -> MatchDecision:
    """Promote one selected TMDB candidate after an explicit user action.

    This never invents a candidate.  Empty, unavailable, or malformed matches
    stay reviewable and cannot enter a write plan.
    """

    selected = decision.selected
    if selected is None and decision.ranked_candidates:
        top = decision.ranked_candidates[0]
        if top.eligible:
            selected = top.candidate
    if decision.status is not MatchStatus.NEEDS_REVIEW or selected is None:
        return decision
    return MatchDecision(
        status=MatchStatus.ACCEPTED,
        selected=selected,
        confidence=MatchConfidence.HIGH,
        score=decision.score,
        margin=decision.margin,
        ranked_candidates=decision.ranked_candidates,
        reasons=tuple(dict.fromkeys((*decision.reasons, MatchReason.MANUAL_LOCK))),
        source=MatchSource.MANUAL,
    )


@dataclass(frozen=True, slots=True)
class ManualMatch:
    """An immutable, previously confirmed identity."""

    candidate: TmdbCandidate


class TmdbMatchClient(Protocol):
    async def search_candidates(
        self, query: MediaMatchInput
    ) -> Sequence[TmdbCandidate] | Mapping[str, object]: ...


class _ConflictingCandidatesError(Exception):
    """Candidates with one TMDB ID disagree on matching fields."""


def build_match_input(
    parsed: MediaParseResult,
    *,
    kind: MediaKind = MediaKind.UNKNOWN,
    origin_country_hints: Sequence[str] = (),
) -> MediaMatchInput:
    """Project parser output into a safe, stable TMDB query shape."""

    if not isinstance(parsed, MediaParseResult):
        raise TypeError("parsed must be MediaParseResult")
    return MediaMatchInput(
        title=_clean_public_text(parsed.title) or None,
        year=parsed.year,
        year_candidates=tuple(sorted(set(parsed.year_candidates))),
        media_type_hint=parsed.media_type_hint,
        kind=_coerce_kind(kind),
        season=parsed.season,
        episode_start=parsed.episode_start,
        episode_end=parsed.episode_end,
        special_hints=_canonical_special_hints(parsed.special_hints),
        origin_country_hints=_canonical_countries(origin_country_hints),
    )


class TmdbMatcher:
    """Deterministic candidate scoring with conservative acceptance gates."""

    def __init__(
        self,
        client: TmdbMatchClient,
        *,
        timeout_seconds: float = 10.0,
        auto_accept_score: int = 85,
        minimum_margin: int = 8,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= auto_accept_score <= 100:
            raise ValueError("auto_accept_score out of range")
        if minimum_margin < 0:
            raise ValueError("minimum_margin must be non-negative")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._auto_accept_score = auto_accept_score
        self._minimum_margin = minimum_margin

    async def match(
        self,
        query: MediaMatchInput | MediaParseResult,
        *,
        manual_lock: ManualMatch | None = None,
    ) -> MatchDecision:
        match_input = (
            build_match_input(query) if isinstance(query, MediaParseResult) else query
        )
        if not isinstance(match_input, MediaMatchInput):
            raise TypeError("query must be MediaMatchInput or MediaParseResult")
        if manual_lock is not None:
            return _locked_decision(manual_lock)
        if not match_input.title:
            return MatchDecision(
                status=MatchStatus.NEEDS_REVIEW,
                reasons=(MatchReason.TITLE_MISSING,),
            )
        try:
            raw = await self._search(match_input)
            candidates = _coerce_candidates(raw)
        except _ConflictingCandidatesError:
            return _status_decision(
                MatchStatus.NEEDS_REVIEW, MatchReason.CONFLICTING_CANDIDATES
            )
        except TmdbRateLimitError:
            return _status_decision(MatchStatus.RATE_LIMITED, MatchReason.RATE_LIMITED)
        except (TmdbTimeoutError, TimeoutError):
            return _status_decision(MatchStatus.TIMEOUT, MatchReason.TIMEOUT)
        except (TmdbMalformedResponseError, ValueError, TypeError, KeyError):
            return _status_decision(
                MatchStatus.MALFORMED_RESPONSE, MatchReason.MALFORMED_RESPONSE
            )
        except TmdbUnavailableError:
            return _status_decision(MatchStatus.UNAVAILABLE, MatchReason.UNAVAILABLE)
        except Exception as error:  # noqa: BLE001 - stable public mapping
            if error.__class__.__name__ in {"TmdbAuthError", "TmdbError"}:
                return _status_decision(
                    MatchStatus.UNAVAILABLE, MatchReason.UNAVAILABLE
                )
            return _status_decision(MatchStatus.UNAVAILABLE, MatchReason.UNAVAILABLE)

        if not candidates:
            return _status_decision(
                MatchStatus.NO_CANDIDATES, MatchReason.EMPTY_CANDIDATES
            )
        ranked = tuple(_rank_candidates(match_input, candidates))
        return _decision_from_ranked(
            ranked, self._auto_accept_score, self._minimum_margin
        )

    async def _search(
        self, query: MediaMatchInput
    ) -> Sequence[TmdbCandidate] | Mapping[str, object]:
        method = getattr(self._client, "search_candidates", None)
        if method is None:
            method = getattr(self._client, "search", None)
        if not callable(method):
            raise TmdbUnavailableError
        response = method(query)
        if not inspect.isawaitable(response):
            raise TmdbMalformedResponseError
        return await asyncio.wait_for(response, timeout=self._timeout_seconds)


def _status_decision(status: MatchStatus, reason: MatchReason) -> MatchDecision:
    return MatchDecision(status=status, reasons=(reason,))


def _locked_decision(lock: ManualMatch) -> MatchDecision:
    evidence = MatchEvidence(score=100, reasons=(MatchReason.MANUAL_LOCK,))
    ranked = RankedCandidate(lock.candidate, 100, evidence, True)
    return MatchDecision(
        status=MatchStatus.ACCEPTED,
        selected=lock.candidate,
        confidence=MatchConfidence.HIGH,
        score=100,
        margin=None,
        ranked_candidates=(ranked,),
        reasons=(MatchReason.MANUAL_LOCK,),
        source=MatchSource.MANUAL,
    )


def _coerce_candidates(
    raw: Sequence[TmdbCandidate] | Mapping[str, object],
) -> tuple[TmdbCandidate, ...]:
    if isinstance(raw, Mapping):
        raw = raw.get("results")  # type: ignore[assignment]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise TmdbMalformedResponseError
    parsed: list[TmdbCandidate] = []
    for item in raw:
        if isinstance(item, TmdbCandidate):
            parsed.append(item)
        elif isinstance(item, Mapping):
            parsed.append(TmdbCandidate.from_payload(item))
        else:
            raise TmdbMalformedResponseError
    by_identity: dict[tuple[int, MediaType], list[TmdbCandidate]] = {}
    for candidate in parsed:
        by_identity.setdefault(candidate.identity, []).append(candidate)

    unique: list[TmdbCandidate] = []
    for identity in sorted(by_identity, key=lambda item: (item[0], item[1].value)):
        group = by_identity[identity]
        fingerprints = {_candidate_conflict_fingerprint(item) for item in group}
        if len(fingerprints) > 1:
            raise _ConflictingCandidatesError
        unique.append(min(group, key=_candidate_canonical_key))
    return tuple(unique)


def _rank_candidates(
    query: MediaMatchInput, candidates: Sequence[TmdbCandidate]
) -> list[RankedCandidate]:
    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        score, evidence, eligible = _score_candidate(query, candidate)
        ranked.append(RankedCandidate(candidate, score, evidence, eligible))
    ranked.sort(key=_rank_key)
    return ranked


def _decision_from_ranked(
    ranked: tuple[RankedCandidate, ...], auto_score: int, minimum_margin: int
) -> MatchDecision:
    if not ranked:
        return _status_decision(MatchStatus.NO_CANDIDATES, MatchReason.EMPTY_CANDIDATES)
    top = ranked[0]
    margin = top.score - ranked[1].score if len(ranked) > 1 else top.score
    reasons = list(top.evidence.reasons)
    if not top.eligible:
        confidence = MatchConfidence.LOW
    elif top.score < auto_score:
        confidence = MatchConfidence.MEDIUM
        reasons.append(MatchReason.LOW_SCORE)
    elif margin < minimum_margin:
        confidence = MatchConfidence.MEDIUM
        reasons.append(MatchReason.SCORE_MARGIN_INSUFFICIENT)
    else:
        return MatchDecision(
            status=MatchStatus.ACCEPTED,
            selected=top.candidate,
            confidence=MatchConfidence.HIGH,
            score=top.score,
            margin=margin,
            ranked_candidates=ranked,
            source=MatchSource.AUTOMATIC,
        )
    return MatchDecision(
        status=MatchStatus.NEEDS_REVIEW,
        confidence=confidence,
        score=top.score,
        margin=margin,
        ranked_candidates=ranked,
        reasons=_unique_reasons(reasons),
        source=MatchSource.AUTOMATIC,
    )


def _score_candidate(
    query: MediaMatchInput, candidate: TmdbCandidate
) -> tuple[int, MatchEvidence, bool]:
    reasons: list[MatchReason] = []
    score = 0
    title_match = _title_match(query.title or "", candidate)
    if title_match is None:
        reasons.append(MatchReason.TITLE_MISMATCH)
    else:
        score += {"title": 55, "original_title": 50, "english_title": 50, "alias": 50}[title_match]

    year_match: str | None = None
    expected_years = (
        {query.year} if query.year is not None else set(query.year_candidates)
    )
    if expected_years:
        if candidate.release_year is None:
            reasons.append(MatchReason.YEAR_UNKNOWN)
        elif _year_in_expected(candidate.release_year, expected_years, query, candidate):
            score += 20
            year_match = (
                "exact" if query.year == candidate.release_year else "candidate"
            )
        else:
            reasons.append(MatchReason.YEAR_CONFLICT)

    media_type_match: bool | None = None
    if query.media_type_hint in {"movie", "tv"}:
        expected_type = MediaType(query.media_type_hint)
        media_type_match = candidate.media_type == expected_type
        if media_type_match:
            score += 15
        else:
            reasons.append(MatchReason.MEDIA_TYPE_CONFLICT)
    else:
        reasons.append(MatchReason.MEDIA_TYPE_UNKNOWN)

    kind_match: bool | None = None
    if query.kind != MediaKind.UNKNOWN:
        if candidate.kind == MediaKind.UNKNOWN:
            reasons.append(MatchReason.KIND_UNKNOWN)
        else:
            kind_match = candidate.kind == query.kind
            if kind_match:
                score += 5
            else:
                reasons.append(MatchReason.KIND_CONFLICT)

    country_match: bool | None = None
    if query.origin_country_hints:
        if not candidate.origin_countries:
            reasons.append(MatchReason.ORIGIN_COUNTRY_UNKNOWN)
        else:
            country_match = bool(
                set(query.origin_country_hints) & set(candidate.origin_countries)
            )
            if country_match:
                score += 5
            else:
                reasons.append(MatchReason.ORIGIN_COUNTRY_CONFLICT)

    season_match, episode_match = _score_season_episode(query, candidate, reasons)
    if season_match:
        score += 5
    if episode_match:
        score += 5

    special_match = _score_special(query, candidate, reasons)
    if special_match:
        score += 5

    score = min(100, score)
    blocking = set(reasons) & _BLOCKING_REASONS
    eligible = title_match is not None and not blocking
    evidence = MatchEvidence(
        score=score,
        title_match=title_match,
        year_match=year_match,
        media_type_match=media_type_match,
        kind_match=kind_match,
        origin_country_match=country_match,
        season_match=season_match,
        episode_match=episode_match,
        special_match=special_match,
        reasons=_unique_reasons(reasons),
    )
    return score, evidence, eligible


_BLOCKING_REASONS = {
    MatchReason.TITLE_MISMATCH,
    MatchReason.YEAR_CONFLICT,
    MatchReason.YEAR_UNKNOWN,
    MatchReason.MEDIA_TYPE_CONFLICT,
    MatchReason.MEDIA_TYPE_UNKNOWN,
    MatchReason.KIND_CONFLICT,
    MatchReason.KIND_UNKNOWN,
    MatchReason.ORIGIN_COUNTRY_CONFLICT,
    MatchReason.ORIGIN_COUNTRY_UNKNOWN,
    MatchReason.SEASON_OUT_OF_RANGE,
    MatchReason.SEASON_UNKNOWN,
    MatchReason.EPISODE_OUT_OF_RANGE,
    MatchReason.EPISODE_RANGE_NOT_COVERED,
    MatchReason.EPISODE_UNKNOWN,
    MatchReason.SPECIAL_CONFLICT,
    MatchReason.SPECIAL_BOUNDARY_UNKNOWN,
}


def _score_season_episode(
    query: MediaMatchInput, candidate: TmdbCandidate, reasons: list[MatchReason]
) -> tuple[bool | None, bool | None]:
    if query.season is None:
        if query.episode_start is not None:
            reasons.append(MatchReason.SEASON_UNKNOWN)
        return None, None
    if not candidate.seasons:
        reasons.append(MatchReason.SEASON_UNKNOWN)
        return False, None
    season = next(
        (item for item in candidate.seasons if item.season_number == query.season), None
    )
    if season is None:
        reasons.append(MatchReason.SEASON_OUT_OF_RANGE)
        return False, False if query.episode_start is not None else None
    episode_match: bool | None = None
    if query.episode_start is not None:
        end = query.episode_end or query.episode_start
        requested = set(range(query.episode_start, end + 1))
        if season.episode_count is None:
            reasons.append(MatchReason.EPISODE_UNKNOWN)
        elif end > season.episode_count:
            reasons.append(MatchReason.EPISODE_OUT_OF_RANGE)
        elif season.episode_numbers and not requested.issubset(season.episode_numbers):
            reasons.append(MatchReason.EPISODE_RANGE_NOT_COVERED)
        else:
            episode_match = True
    return True, episode_match


def _score_special(
    query: MediaMatchInput, candidate: TmdbCandidate, reasons: list[MatchReason]
) -> bool | None:
    requested = set(_canonical_special_hints(query.special_hints))
    actual = candidate.special_kind
    if not requested:
        if actual not in {SpecialKind.STANDARD, SpecialKind.UNKNOWN}:
            reasons.append(MatchReason.SPECIAL_CONFLICT)
        return None
    if actual == SpecialKind.UNKNOWN:
        reasons.append(MatchReason.SPECIAL_BOUNDARY_UNKNOWN)
        return False
    if actual == SpecialKind.STANDARD:
        reasons.append(MatchReason.SPECIAL_CONFLICT)
        return False
    if "special" in requested or actual.value in requested:
        return True
    reasons.append(MatchReason.SPECIAL_CONFLICT)
    return False


def _title_match(title: str, candidate: TmdbCandidate) -> str | None:
    normalized = _normalize(title)
    options = (
        (candidate.title, "title"),
        (candidate.original_title, "original_title"),
        (candidate.english_title, "english_title"),
    )
    options += tuple((alias, "alias") for alias in candidate.aliases)
    # A multi-token query (e.g. "末日地堡 Silo" from a localized+original
    # title pair) that fails whole-query equality also matches when any
    # single token is a full, exact equal of a title field. Tokens are
    # compared whole against whole fields, never as substrings, so "the"
    # can never match "The Boys".
    tokens = normalized.split()
    use_tokens = len(tokens) >= 2 and all(len(token) >= 2 for token in tokens)
    for value, kind in options:
        if not value:
            continue
        field = _normalize(value)
        if field == normalized or (
            use_tokens and any(token == field for token in tokens)
        ):
            return kind
    return None


def _year_in_expected(
    release_year: int,
    expected_years: set[int],
    query: MediaMatchInput,
    candidate: TmdbCandidate,
) -> bool:
    if release_year in expected_years:
        return True
    # A TV file's year is the season airing year and may differ from the
    # series premiere year by a year or so. Movies must match strictly.
    return (
        query.media_type_hint == "tv"
        and candidate.media_type is MediaType.TV
        and any(abs(release_year - year) <= 1 for year in expected_years)
    )


def _rank_key(item: RankedCandidate) -> tuple[object, ...]:
    candidate = item.candidate
    return (
        -item.score,
        candidate.tmdb_id,
        candidate.media_type.value,
        _normalize(candidate.title),
        _normalize(candidate.original_title or ""),
    )


def _candidate_conflict_fingerprint(candidate: TmdbCandidate) -> tuple[object, ...]:
    return (
        candidate.media_type.value,
        _normalize(candidate.title),
        _normalize(candidate.original_title or ""),
        tuple(sorted(_normalize(alias) for alias in candidate.aliases)),
        candidate.release_year,
        candidate.origin_countries,
        candidate.kind.value,
        candidate.special_kind.value,
        tuple(
            (
                season.season_number,
                season.episode_count,
                season.episode_numbers,
            )
            for season in candidate.seasons
        ),
    )


def _candidate_canonical_key(candidate: TmdbCandidate) -> tuple[object, ...]:
    """Choose one stable representative among equivalent candidate payloads."""

    return (
        candidate.media_type.value,
        candidate.tmdb_id,
        _stable_text_key(candidate.title),
        _stable_text_key(candidate.original_title or ""),
        tuple(_stable_text_key(alias) for alias in candidate.aliases),
        candidate.release_year if candidate.release_year is not None else -1,
        candidate.origin_countries,
        candidate.kind.value,
        candidate.special_kind.value,
        tuple(_season_canonical_key(season) for season in candidate.seasons),
    )


def _dedupe_seasons(seasons: Sequence[TmdbSeason]) -> tuple[TmdbSeason, ...]:
    by_number: dict[int, list[TmdbSeason]] = {}
    for season in seasons:
        if not isinstance(season, TmdbSeason):
            raise TypeError("invalid season")
        by_number.setdefault(season.season_number, []).append(season)
    result: list[TmdbSeason] = []
    for number in sorted(by_number):
        group = by_number[number]
        if len({_season_fingerprint(item) for item in group}) > 1:
            raise ValueError("conflicting season metadata")
        result.append(min(group, key=_season_canonical_key))
    return tuple(result)


def _season_fingerprint(season: TmdbSeason) -> tuple[object, ...]:
    return season.season_number, season.episode_count, season.episode_numbers


def _season_canonical_key(season: TmdbSeason) -> tuple[object, ...]:
    return (
        season.season_number,
        season.episode_count if season.episode_count is not None else -1,
        season.episode_numbers,
    )


def _stable_text_key(value: str) -> tuple[str, str, str]:
    return value.casefold(), _normalize(value), value


def _canonical_special_hints(values: Sequence[str]) -> tuple[str, ...]:
    aliases = {
        "sp": "sp",
        "ova": "ova",
        "oad": "oad",
        "ona": "ona",
        "special": "special",
    }
    return tuple(
        sorted(
            {
                aliases[value.strip().casefold()]
                for value in values
                if isinstance(value, str) and value.strip().casefold() in aliases
            }
        )
    )


def _canonical_countries(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip().upper() for value in values if value.strip()}))


def _coerce_kind(value: MediaKind | str) -> MediaKind:
    if isinstance(value, MediaKind):
        return value
    try:
        return MediaKind(value)
    except (TypeError, ValueError):
        return MediaKind.UNKNOWN


def _coerce_special(value: SpecialKind | str) -> SpecialKind:
    if isinstance(value, SpecialKind):
        return value
    normalized = str(value).strip().casefold()
    return {
        "sp": SpecialKind.SP,
        "ova": SpecialKind.OVA,
        "oad": SpecialKind.OAD,
        "ona": SpecialKind.ONA,
        "special": SpecialKind.UNKNOWN,
        "standard": SpecialKind.STANDARD,
    }.get(normalized, SpecialKind.UNKNOWN)


def _clean_public_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in value.strip()
        if not unicodedata.category(character).startswith("C")
    )[:200]


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def _unique_reasons(values: Sequence[MatchReason]) -> tuple[MatchReason, ...]:
    return tuple(dict.fromkeys(values))


def _parse_year(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1800 <= value <= 2200:
        return value
    if isinstance(value, str):
        match = re.match(r"^(?:19|20)\d{2}", value.strip())
        if match:
            return int(match.group())
    return None


def _parse_seasons(value: object) -> tuple[TmdbSeason, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TmdbMalformedResponseError
    seasons: list[TmdbSeason] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TmdbMalformedResponseError
        number = item.get("season_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise TmdbMalformedResponseError
        count = item.get("episode_count")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool)
        ):
            raise TmdbMalformedResponseError
        episodes = item.get("episode_numbers", ())
        if not isinstance(episodes, (list, tuple)) or any(
            not isinstance(episode, int) or isinstance(episode, bool)
            for episode in episodes
        ):
            raise TmdbMalformedResponseError
        seasons.append(TmdbSeason(number, count, tuple(episodes)))
    return tuple(seasons)
