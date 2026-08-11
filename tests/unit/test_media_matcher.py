from __future__ import annotations

import asyncio

import pytest

from watch_assistant.schemas import MediaType
from watch_assistant.services.media_matcher import (
    ManualMatch,
    MatchConfidence,
    MatchEvidence,
    MatchReason,
    MatchStatus,
    MediaKind,
    MediaMatchInput,
    RankedCandidate,
    SpecialKind,
    TmdbCandidate,
    TmdbMalformedResponseError,
    TmdbMatcher,
    TmdbRateLimitError,
    TmdbSeason,
    TmdbTimeoutError,
    _decision_from_ranked,
    build_match_input,
)
from watch_assistant.services.media_parser import MediaParseResult, parse_media_filename


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
    # Year 2003 is a 2-year gap from the query (2005) so the duplicate stays
    # in year conflict under the TV +/-1 tolerance instead of tying.
    client = FakeClient([candidate(2, year=2003), candidate(1, title="The Office")])
    first = await TmdbMatcher(client).match(query())
    second = await TmdbMatcher(FakeClient(list(reversed(client.response)))).match(
        query()
    )

    assert first.status is MatchStatus.ACCEPTED
    assert first.confidence is MatchConfidence.HIGH
    assert first.selected is not None and first.selected.tmdb_id == 1
    assert first == second


async def test_same_title_different_year_is_review_only():
    # A 2-year gap still conflicts: TV tolerates only +/-1 airing-year drift.
    decision = await TmdbMatcher(FakeClient([candidate(year=2007)])).match(query())

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


async def test_season_zero_specials_require_candidate_season_zero():
    # S00E01 是 TMDB 特辑季 (season 0) 的真实集号:候选含第 0 季时接受,
    # 不含时按季越界处理,与 episode_coverage 的 season 0 语义一致。
    good = candidate(
        seasons=(
            TmdbSeason(0, episode_count=2, episode_numbers=(1, 2)),
            TmdbSeason(1, episode_count=10, episode_numbers=tuple(range(1, 11))),
        )
    )
    accepted = await TmdbMatcher(FakeClient([good])).match(
        query(season=0, episode_start=1, episode_end=2)
    )
    out_of_range = await TmdbMatcher(
        FakeClient([candidate(seasons=(TmdbSeason(1, episode_count=10),))])
    ).match(query(season=0, episode_start=1))

    assert accepted.accepted
    assert out_of_range.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.SEASON_OUT_OF_RANGE in out_of_range.reasons


async def test_e00_special_parse_never_claims_an_episode_number():
    # SxxE00 是特辑占位符:解析结果不得产生集数主张 (下游契约要求
    # 集数 >= 1),并携带特辑提示要求人工确认,与 SP/OVA 处理一致。
    parsed = parse_media_filename("Show.S01E00.mkv")
    match_input = build_match_input(parsed)
    decision = await TmdbMatcher(
        FakeClient([candidate(seasons=(TmdbSeason(1, episode_count=10),))])
    ).match(match_input)

    assert match_input.episode_start is None
    assert match_input.episode_end is None
    assert match_input.special_hints == ("special",)
    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.SPECIAL_CONFLICT in decision.reasons


async def test_year_between_markers_feeds_full_range_into_matching():
    # 年份夹中间的多集命名 (S01E01.2020.E02) 恢复为 E01-E02 范围后,
    # matcher 按完整范围校验候选季的覆盖情况。
    parsed = parse_media_filename("Show.S01E01.2020.E02.mkv")
    match_input = build_match_input(parsed)
    covered = await TmdbMatcher(
        FakeClient(
            [
                candidate(
                    title="Show",
                    year=2020,
                    seasons=(
                        TmdbSeason(1, episode_count=2, episode_numbers=(1, 2)),
                    ),
                )
            ]
        )
    ).match(match_input)
    uncovered = await TmdbMatcher(
        FakeClient(
            [
                candidate(
                    title="Show",
                    year=2020,
                    seasons=(
                        TmdbSeason(1, episode_count=1, episode_numbers=(1,)),
                    ),
                )
            ]
        )
    ).match(match_input)

    assert match_input.season == 1
    assert match_input.episode_start == 1
    assert match_input.episode_end == 2
    assert covered.accepted
    assert uncovered.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.EPISODE_OUT_OF_RANGE in uncovered.reasons


async def test_low_margin_is_needs_review():
    candidates = [candidate(1), candidate(2, title="The Office", year=2005)]
    decision = await TmdbMatcher(FakeClient(candidates), minimum_margin=8).match(
        query()
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.SCORE_MARGIN_INSUFFICIENT in decision.reasons


async def test_conflicting_duplicate_identity_is_order_independent_and_review_only():
    first_candidate = candidate(7, year=2005, title="The Office")
    conflicting_candidate = candidate(7, year=2006, title="The Office")

    first = await TmdbMatcher(
        FakeClient([first_candidate, conflicting_candidate])
    ).match(query())
    reversed_decision = await TmdbMatcher(
        FakeClient([conflicting_candidate, first_candidate])
    ).match(query())

    assert first.status is MatchStatus.NEEDS_REVIEW
    assert first.reasons == (MatchReason.CONFLICTING_CANDIDATES,)
    assert first == reversed_decision


async def test_movie_and_tv_share_numeric_id_but_are_distinct_identities():
    movie = candidate(7, title="The Office", media_type=MediaType.MOVIE)
    tv = candidate(7, title="The Office", media_type=MediaType.TV)

    tv_first = await TmdbMatcher(FakeClient([movie, tv])).match(query())
    tv_reversed = await TmdbMatcher(FakeClient([tv, movie])).match(query())
    movie_first = await TmdbMatcher(FakeClient([movie, tv])).match(
        query(media_type_hint="movie")
    )
    movie_reversed = await TmdbMatcher(FakeClient([tv, movie])).match(
        query(media_type_hint="movie")
    )

    assert tv_first.accepted and tv_first.selected is not None
    assert tv_first.selected.media_type is MediaType.TV
    assert movie_first.accepted and movie_first.selected is not None
    assert movie_first.selected.media_type is MediaType.MOVIE
    assert tv_first == tv_reversed
    assert movie_first == movie_reversed


async def test_equivalent_duplicate_identity_is_deterministically_deduped():
    duplicate = candidate(7, title="The.Office", aliases=("办公室", "US OFFICE"))
    same = candidate(7, title="The Office", aliases=("us office", "办公室"))

    decision = await TmdbMatcher(FakeClient([duplicate, same])).match(query())
    reversed_decision = await TmdbMatcher(FakeClient([same, duplicate])).match(query())

    assert decision.accepted
    assert len(decision.ranked_candidates) == 1
    assert decision.selected is not None
    assert decision.selected.title == "The Office"
    assert decision == reversed_decision


async def test_equivalent_duplicate_seasons_are_deterministically_deduped():
    first = candidate(
        7,
        seasons=(
            TmdbSeason(2, episode_count=3, episode_numbers=(1, 2, 3)),
            TmdbSeason(1, episode_count=2, episode_numbers=(1, 2)),
            TmdbSeason(1, episode_count=2, episode_numbers=(1, 2)),
        ),
    )
    reversed_seasons = candidate(
        7,
        seasons=(
            TmdbSeason(1, episode_count=2, episode_numbers=(2, 1)),
            TmdbSeason(2, episode_count=3, episode_numbers=(3, 2, 1)),
        ),
    )

    assert first.seasons == reversed_seasons.seasons
    assert first.seasons == (
        TmdbSeason(1, episode_count=2, episode_numbers=(1, 2)),
        TmdbSeason(2, episode_count=3, episode_numbers=(1, 2, 3)),
    )

    first_decision = await TmdbMatcher(FakeClient([first])).match(
        query(season=2, episode_start=2, episode_end=3)
    )
    reversed_decision = await TmdbMatcher(FakeClient([reversed_seasons])).match(
        query(season=2, episode_start=2, episode_end=3)
    )
    assert first_decision.accepted
    assert first_decision == reversed_decision


@pytest.mark.parametrize(
    "seasons",
    (
        (TmdbSeason(1, episode_count=2), TmdbSeason(1, episode_count=3)),
        (TmdbSeason(1, episode_numbers=(1, 2)), TmdbSeason(1, episode_numbers=(1, 3))),
    ),
)
def test_conflicting_duplicate_seasons_fail_closed(seasons):
    with pytest.raises(ValueError, match="conflicting season metadata"):
        candidate(7, seasons=seasons)


@pytest.mark.parametrize(
    "left,right",
    (
        (candidate(7, title="The Office"), candidate(7, title="Office")),
        (
            candidate(7, seasons=(TmdbSeason(1, episode_count=10),)),
            candidate(7, seasons=(TmdbSeason(1, episode_count=8),)),
        ),
        (
            candidate(7, special=SpecialKind.STANDARD),
            candidate(7, special=SpecialKind.SP),
        ),
    ),
)
async def test_duplicate_identity_field_conflicts_fail_closed(left, right):
    first = await TmdbMatcher(FakeClient([left, right])).match(query())
    reversed_decision = await TmdbMatcher(FakeClient([right, left])).match(query())

    assert first.status is MatchStatus.NEEDS_REVIEW
    assert first.reasons == (MatchReason.CONFLICTING_CANDIDATES,)
    assert first == reversed_decision


@pytest.mark.parametrize(
    ("query_title", "candidate_title"),
    (("Office", "The Office"), ("办公室", "办公室风云")),
)
async def test_substring_titles_are_not_strong_matches(query_title, candidate_title):
    decision = await TmdbMatcher(FakeClient([candidate(title=candidate_title)])).match(
        query(title=query_title)
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.TITLE_MISMATCH in decision.reasons


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
    with pytest.raises(asyncio.CancelledError):
        await TmdbMatcher(FakeClient(asyncio.CancelledError())).match(query())
    malformed = await TmdbMatcher(FakeClient({"unexpected": []})).match(query())

    assert malformed.status is MatchStatus.MALFORMED_RESPONSE

    with pytest.raises(TmdbMalformedResponseError):
        TmdbCandidate.from_payload({"id": 1, "media_type": "movie"})


async def test_empty_and_duplicate_candidates():
    empty = await TmdbMatcher(FakeClient([])).match(query())
    duplicate = await TmdbMatcher(
        FakeClient([candidate(), candidate(title="The Office", aliases=("Office",))])
    ).match(query())

    assert empty.status is MatchStatus.NO_CANDIDATES
    assert duplicate.status is MatchStatus.NEEDS_REVIEW
    assert duplicate.reasons == (MatchReason.CONFLICTING_CANDIDATES,)


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


def test_english_title_matches_an_english_filename():
    """English filenames (common in torrents) must match the English title."""
    candidate = TmdbCandidate(
        tmdb_id=1311031,
        media_type=MediaType.MOVIE,
        title="鬼灭之刃：无限城篇 第一章 猗窝座再袭",
        original_title="劇場版「鬼滅の刃」無限城編 第一章 猗窩座再来",
        english_title="Demon Slayer: Kimetsu no Yaiba Infinity Castle",
        release_year=2025,
        origin_countries=("JP",),
        kind=MediaKind.MOVIE,
    )
    match_input = build_match_input(
        MediaParseResult(
            original_filename="Demon Slayer Kimetsu No Yaiba Infinity Castle (2025).mkv",
            title="Demon Slayer Kimetsu No Yaiba Infinity Castle",
            year=2025,
            media_type_hint="movie",
        )
    )
    decision = asyncio.run(TmdbMatcher(FakeClient([candidate])).match(match_input))
    assert decision.status is MatchStatus.ACCEPTED
    assert decision.confidence is MatchConfidence.HIGH


async def test_multi_token_query_matches_localized_and_original_title_pair():
    """A localized+original title pair query matches either token exactly."""
    silo = TmdbCandidate(
        tmdb_id=125988,
        media_type=MediaType.TV,
        title="末日地堡",
        original_title="Silo",
        release_year=2023,
        seasons=(
            TmdbSeason(
                2,
                episode_count=8,
                episode_numbers=(1, 2, 3, 4, 5, 6, 7, 8),
            ),
        ),
    )
    decision = await TmdbMatcher(FakeClient([silo])).match(
        query(title="末日地堡 Silo", year=2024, season=2, episode_start=1)
    )

    assert decision.status is MatchStatus.ACCEPTED
    assert decision.confidence is MatchConfidence.HIGH
    assert decision.selected is not None and decision.selected.tmdb_id == 125988
    assert decision.score == 100
    evidence = decision.ranked_candidates[0].evidence
    assert evidence.title_match == "title"
    assert evidence.year_match == "candidate"


@pytest.mark.parametrize("query_title", ("The Boys Diabolical", "Boys"))
async def test_title_tokens_are_never_partial_matches(query_title):
    """No single token may match a multi-word field (exact equality only)."""
    decision = await TmdbMatcher(FakeClient([candidate(title="The Boys")])).match(
        query(title=query_title)
    )

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.TITLE_MISMATCH in decision.reasons


async def test_tv_year_one_year_off_is_tolerated():
    """A TV file's year is the season airing year, not the premiere year."""
    decision = await TmdbMatcher(FakeClient([candidate(title="Silo", year=2023)])).match(
        query(title="Silo", year=2024)
    )

    assert decision.status is MatchStatus.ACCEPTED
    assert decision.confidence is MatchConfidence.HIGH
    assert MatchReason.YEAR_CONFLICT not in decision.reasons
    assert decision.ranked_candidates[0].evidence.year_match == "candidate"


async def test_movie_year_still_matches_strictly():
    """Movies keep strict year matching; the TV tolerance never applies."""
    decision = await TmdbMatcher(
        FakeClient(
            [
                candidate(
                    title="Once Upon a Time in Hollywood",
                    media_type=MediaType.MOVIE,
                    year=2018,
                )
            ]
        )
    ).match(query(title="Once Upon a Time in Hollywood", year=2019, media_type_hint="movie"))

    assert decision.status is MatchStatus.NEEDS_REVIEW
    assert MatchReason.YEAR_CONFLICT in decision.reasons


def test_no_year_tv_with_full_structure_match_is_accepted():
    """无年份的剧集(标题/季集全匹配+唯一候选)自动接受,不再停留待办。"""
    candidate = TmdbCandidate(
        tmdb_id=259837,
        media_type=MediaType.TV,
        title="超能路人甲",
        kind=MediaKind.TV,
        release_year=2026,
        origin_countries=("KR",),
    )
    evidence = MatchEvidence(
        score=75,
        title_match="original_title",
        year_match=None,
        media_type_match=True,
        season_match=True,
        episode_match=True,
    )
    decision = _decision_from_ranked((RankedCandidate(candidate, 75, evidence, True),), 85, 8)
    assert decision.status is MatchStatus.ACCEPTED
    assert decision.selected is candidate


def test_no_year_tv_without_episode_match_stays_review():
    """结构不完整(集数不匹配)时保持待办,不因豁免误接受。"""
    candidate = TmdbCandidate(
        tmdb_id=999,
        media_type=MediaType.TV,
        title="Some Show",
        kind=MediaKind.TV,
        release_year=2024,
        origin_countries=("US",),
    )
    evidence = MatchEvidence(
        score=75,
        title_match="title",
        year_match=None,
        media_type_match=True,
        season_match=True,
        episode_match=False,
    )
    decision = _decision_from_ranked((RankedCandidate(candidate, 75, evidence, True),), 85, 8)
    assert decision.status is MatchStatus.NEEDS_REVIEW
