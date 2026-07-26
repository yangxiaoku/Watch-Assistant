from __future__ import annotations

import asyncio

import pytest

from watch_assistant.schemas import MediaType
from watch_assistant.services.media_matcher import (
    ManualMatch,
    MatchConfidence,
    MatchReason,
    MatchStatus,
    MediaKind,
    MediaMatchInput,
    SpecialKind,
    TmdbCandidate,
    TmdbMalformedResponseError,
    TmdbMatcher,
    TmdbRateLimitError,
    TmdbSeason,
    TmdbTimeoutError,
    build_match_input,
)
from watch_assistant.services.media_parser import MediaParseResult


def candidate(
    tmdb_id: int = 1,
    *,
    title: str = "The Office",
    media_type: MediaType = MediaType.TV,
    year: int | None = 2005,
    aliases: tuple[str, ...] = (),
    kind: MediaKind = MediaKind.UNKNOWN,
    countries: tuple[str, ...] = (),
    seasons: tuple[TmdbSeason, ...] = (),
    special: SpecialKind = SpecialKind.STANDARD,
) -> TmdbCandidate:
    return TmdbCandidate(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        release_year=year,
        aliases=aliases,
        kind=kind,
        origin_countries=countries,
        seasons=seasons,
        special_kind=special,
    )


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def search_candidates(self, query):
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def query(**changes) -> MediaMatchInput:
    values = {
        "title": "The Office",
        "year": 2005,
        "media_type_hint": "tv",
    }
    values.update(changes)
    return MediaMatchInput(**values)


@pytest.mark.parametrize(
    ("title", "aliases"),
    (
        ("办公室", ("The Office", "办公室")),
        ("進撃の巨人", ("Attack on Titan", "進撃の巨人")),
        ("오징어 게임", ("Squid Game", "오징어 게임")),
    ),
)
async def test_aliases_are_supported_for_multiple_scripts(title, aliases):
    client = FakeClient([candidate(aliases=aliases)])
    decision = await TmdbMatcher(client).match(query(title=title))

    assert decision.accepted
    assert decision.selected is not None
    assert decision.selected.tmdb_id == 1


async def test_high_confidence_acceptance_and_stable_tie_breaking():
    client = FakeClient([candidate(2, year=2004), candidate(1, title="The Office")])
    first = await TmdbMatcher(client).match(query())
    second = await TmdbMatcher(FakeClient(list(reversed(client.response)))).match(
        query()
    )

    assert first.status is MatchStatus.ACCEPTED
    assert first.confidence is MatchConfidence.HIGH
    assert first.selected is not None and first.selected.tmdb_id == 1
    assert first == second


async def test_same_title_different_year_is_review_only():
    decision = await TmdbMatcher(FakeClient([candidate(year=2006)])).match(query())

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.YEAR_CONFLICT in decision.reasons


async def test_numeric_title_year_conflict_does_not_use_title_year_as_identity():
    parsed = MediaParseResult(
        title="1917", year=2019, year_candidates=(1917, 2019), media_type_hint="movie"
    )
    match_input = build_match_input(parsed)
    decision = await TmdbMatcher(
        FakeClient(
            [
                candidate(
                    title="1917",
                    media_type=MediaType.MOVIE,
                    year=1917,
                )
            ]
        )
    ).match(match_input)

    assert match_input.title == "1917"
    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.YEAR_CONFLICT in decision.reasons


@pytest.mark.parametrize(
    ("candidate_kind", "query_kind"),
    (
        (MediaKind.MOVIE, MediaKind.DOCUMENTARY),
        (MediaKind.VARIETY, MediaKind.ANIME),
    ),
)
async def test_kind_conflict_blocks_auto_acceptance(candidate_kind, query_kind):
    decision = await TmdbMatcher(FakeClient([candidate(kind=candidate_kind)])).match(
        query(kind=query_kind)
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.KIND_CONFLICT in decision.reasons


async def test_country_intersection_is_evidence_but_unknown_country_requires_review():
    accepted = await TmdbMatcher(FakeClient([candidate(countries=("US", "CA"))])).match(
        query(origin_country_hints=("CA",))
    )
    unknown = await TmdbMatcher(FakeClient([candidate()])).match(
        query(origin_country_hints=("CA",))
    )

    assert accepted.accepted
    assert unknown.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.ORIGIN_COUNTRY_UNKNOWN in unknown.reasons


@pytest.mark.parametrize(
    "seasons",
    (
        (),
        (TmdbSeason(1, episode_count=3),),
    ),
)
async def test_season_and_episode_boundaries_require_confirmed_coverage(seasons):
    decision = await TmdbMatcher(FakeClient([candidate(seasons=seasons)])).match(
        query(season=1, episode_start=1, episode_end=4)
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert any(
        reason in decision.reasons
        for reason in (
            MatchReason.SEASON_UNKNOWN,
            MatchReason.EPISODE_OUT_OF_RANGE,
        )
    )


async def test_episode_range_and_special_boundary_must_match():
    good = candidate(
        seasons=(TmdbSeason(1, episode_count=4, episode_numbers=(1, 2, 3, 4)),),
        special=SpecialKind.SP,
    )
    accepted = await TmdbMatcher(FakeClient([good])).match(
        query(season=1, episode_start=2, episode_end=3, special_hints=("SP",))
    )
    conflict = await TmdbMatcher(
        FakeClient([candidate(seasons=good.seasons, special=SpecialKind.STANDARD)])
    ).match(query(season=1, episode_start=2, special_hints=("OVA",)))

    assert accepted.accepted
    assert conflict.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.SPECIAL_CONFLICT in conflict.reasons


async def test_low_margin_is_needs_review():
    candidates = [candidate(1), candidate(2, title="The Office", year=2005)]
    decision = await TmdbMatcher(FakeClient(candidates), minimum_margin=8).match(
        query()
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.SCORE_MARGIN_INSUFFICIENT in decision.reasons


@pytest.mark.parametrize(
    ("error", "status"),
    (
        (TmdbRateLimitError(), MatchStatus.RATE_LIMITED),
        (TmdbTimeoutError(), MatchStatus.TIMEOUT),
        (RuntimeError("bad"), MatchStatus.UNAVAILABLE),
    ),
)
async def test_client_failures_are_distinct_and_safe(error, status):
    decision = await TmdbMatcher(FakeClient(error)).match(query())

    assert decision.status is status
    assert "bad" not in repr(decision)


async def test_timeout_and_exception_representations_are_redacted():
    async def slow_search(_query):
        await asyncio.sleep(0.05)
        return []

    class SlowClient:
        async def search_candidates(self, query):
            return await slow_search(query)

    decision = await TmdbMatcher(SlowClient(), timeout_seconds=0.001).match(query())
    error = TmdbRateLimitError("private/path and api-key")

    assert decision.status is MatchStatus.TIMEOUT
    assert str(error) == "TmdbRateLimitError"
    assert "private/path" not in repr(error)


async def test_cancelled_and_malformed_responses_are_distinct():
    cancelled = await TmdbMatcher(FakeClient(asyncio.CancelledError())).match(query())
    malformed = await TmdbMatcher(FakeClient({"unexpected": []})).match(query())

    assert cancelled.status is MatchStatus.CANCELLED
    assert malformed.status is MatchStatus.MALFORMED_RESPONSE

    with pytest.raises(TmdbMalformedResponseError):
        TmdbCandidate.from_payload({"id": 1, "media_type": "movie"})


async def test_empty_and_duplicate_candidates():
    empty = await TmdbMatcher(FakeClient([])).match(query())
    duplicate = await TmdbMatcher(
        FakeClient([candidate(), candidate(title="The Office", aliases=("Office",))])
    ).match(query())

    assert empty.status is MatchStatus.NO_CANDIDATES
    assert len(duplicate.ranked_candidates) == 1


async def test_manual_lock_wins_without_calling_tmdb():
    client = FakeClient([candidate(99, year=2005)])
    locked = candidate(42, title="Locked", media_type=MediaType.MOVIE, year=1999)
    decision = await TmdbMatcher(client).match(query(), manual_lock=ManualMatch(locked))

    assert decision.accepted
    assert decision.source.value == "manual"
    assert decision.selected == locked
    assert client.calls == 0


def test_payload_and_parser_projection_are_safe_and_bounded():
    payload = {
        "id": 9,
        "media_type": "tv",
        "name": "安全标题",
        "alternative_titles": {"results": [{"title": "安全别名"}]},
        "first_air_date": "2020-01-01",
        "origin_country": ["JP", "US"],
    }
    parsed = build_match_input(
        MediaParseResult(
            original_filename="C:/private/secret.mkv",
            title="安全标题",
            year=2020,
            media_type_hint="tv",
        )
    )
    candidate_from_payload = TmdbCandidate.from_payload(payload)

    assert candidate_from_payload.aliases == ("安全别名",)
    assert "secret" not in repr(parsed)
    assert "secret" not in repr(candidate_from_payload)
    assert "api-key" not in repr(candidate_from_payload)
