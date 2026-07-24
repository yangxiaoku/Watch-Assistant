from copy import deepcopy

import pytest

from watch_assistant.services.episode_coverage import (
    CoverageStatus,
    analyze_episode_coverage,
)


@pytest.mark.parametrize(
    ("name", "episodes"),
    [
        ("Show.S02E01.mkv", (1,)),
        ("Show.S02E01E02.mkv", (1, 2)),
        ("Show.S02E01-E03.mkv", (1, 2, 3)),
        ("Show.S02E01-03.mkv", (1, 2, 3)),
        ("Show.S02E01~E03.mkv", (1, 2, 3)),
        ("Show.2x01.mkv", (1,)),
        ("Show.2x01-03.mkv", (1, 2, 3)),
        ("Show.Season 2 Episode 1.mkv", (1,)),
        ("Show.Season 2 E01.mkv", (1,)),
        ("Show.第2季第1集.mkv", (1,)),
    ],
)
def test_named_formats(name: str, episodes: tuple[int, ...]):
    result = analyze_episode_coverage(
        [name], selected_season=2, expected_episode_count=None
    )

    assert result.detected_seasons == (2,)
    assert result.episodes_found == episodes


@pytest.mark.parametrize(
    "name",
    ["Show.E01.mkv", "Show.Episode 1.mkv", "Show.第1集.mkv"],
)
def test_selected_season_accepts_unseasoned_episode_markers(name: str):
    result = analyze_episode_coverage(
        [name], selected_season=2, expected_episode_count=1
    )

    assert result.status is CoverageStatus.COMPLETE
    assert result.detected_seasons == ()
    assert result.episodes_found == (1,)


def test_paths_nfkc_ranges_duplicates_and_input_order():
    names = [
        r"C:\library\Ｓ０２Ｅ０３.mkv",
        "/library/Show.S02E01-E02.mkv",
        "Show.S02E02.mkv",
    ]
    original = deepcopy(names)

    result = analyze_episode_coverage(
        names, selected_season=2, expected_episode_count=3
    )

    assert result.status is CoverageStatus.COMPLETE
    assert result.detected_seasons == (2,)
    assert result.episodes_found == (1, 2, 3)
    assert names == original

    reversed_result = analyze_episode_coverage(
        list(reversed(names)), selected_season=2, expected_episode_count=3
    )
    assert reversed_result == result


def test_season_zero_and_multiple_season_filtering():
    specials = analyze_episode_coverage(
        ["Show.S00E01.mkv", "Show.0x02.mkv"],
        selected_season=0,
        expected_episode_count=2,
    )
    assert specials.status is CoverageStatus.COMPLETE
    assert specials.detected_seasons == (0,)
    assert specials.episodes_found == (1, 2)

    multiple = analyze_episode_coverage(
        ["Show.S01E01.mkv", "Show.S02E02.mkv"],
        selected_season=2,
        expected_episode_count=2,
    )
    assert multiple.status is CoverageStatus.MULTI_SEASON
    assert multiple.detected_seasons == (1, 2)
    assert multiple.episodes_found == (2,)

    all_seasons = analyze_episode_coverage(
        ["Show.S02 Seasons 1-3.mkv"],
        selected_season=2,
        expected_episode_count=None,
    )
    assert all_seasons.status is CoverageStatus.MULTI_SEASON
    assert all_seasons.detected_seasons == (1, 2, 3)
    assert all_seasons.episodes_found == ()


def test_statuses_and_expected_gaps():
    complete = analyze_episode_coverage(
        ["Show.S02E01.mkv", "Show.S02E02.mkv", "Show.S02E03.mkv"],
        selected_season=2,
        expected_episode_count=3,
    )
    assert complete.status is CoverageStatus.COMPLETE
    assert complete.missing_episodes == ()

    partial = analyze_episode_coverage(
        ["Show.S02E01.mkv", "Show.S02E03.mkv"],
        selected_season=2,
        expected_episode_count=3,
    )
    assert partial.status is CoverageStatus.PARTIAL
    assert partial.missing_episodes == (2,)

    single = analyze_episode_coverage(
        ["Show.S02E02.mkv"], selected_season=2, expected_episode_count=None
    )
    assert single.status is CoverageStatus.SINGLE_EPISODE

    unknown = analyze_episode_coverage(
        ["Show.S02E01.mkv", "Show.S02E02.mkv"],
        selected_season=2,
        expected_episode_count=None,
    )
    assert unknown.status is CoverageStatus.UNKNOWN

    extra = analyze_episode_coverage(
        ["Show.S02E01E02.mkv"], selected_season=2, expected_episode_count=1
    )
    assert extra.status is CoverageStatus.COMPLETE
    assert extra.extra_episodes == (2,)


def test_expected_one_requires_episode_one_for_complete():
    result = analyze_episode_coverage(
        ["Show.S02E02.mkv"], selected_season=2, expected_episode_count=1
    )

    assert result.status is CoverageStatus.SINGLE_EPISODE
    assert result.missing_episodes == (1,)


@pytest.mark.parametrize(
    "name",
    [
        "Show.2012.1080p.x264.H264.mkv",
        "Show.2024.2160p.mkv",
        "Show.hash1234567890.mkv",
        "Show.E2012.mkv",
        "Show.720p.mkv",
        "2024.mkv",
    ],
)
def test_common_numbers_and_codecs_are_not_episode_markers(name: str):
    result = analyze_episode_coverage(
        [name], selected_season=2, expected_episode_count=None
    )

    assert result.status is CoverageStatus.UNKNOWN
    assert result.detected_seasons == ()
    assert result.episodes_found == ()


@pytest.mark.parametrize(
    "name",
    [
        "Show.S02E03-E01.mkv",
        "Show.S02E01-E202.mkv",
        "Show.S02E01-E999.mkv",
        "Show.S02E01-E2012.mkv",
    ],
)
def test_invalid_or_oversized_ranges_are_ignored(name: str):
    result = analyze_episode_coverage(
        [name], selected_season=2, expected_episode_count=None
    )

    assert result.status is CoverageStatus.UNKNOWN
    assert result.episodes_found == ()


def test_plain_numbered_basename_is_the_only_unmarked_episode_form():
    result = analyze_episode_coverage(
        ["01.mkv"], selected_season=2, expected_episode_count=None
    )

    assert result.status is CoverageStatus.SINGLE_EPISODE
    assert result.episodes_found == (1,)


def test_result_does_not_expose_paths_and_validates_arguments():
    path = r"C:\private\Show.S02E01.secret.mkv"
    result = analyze_episode_coverage(
        [path], selected_season=2, expected_episode_count=None
    )

    assert path not in repr(result)
    with pytest.raises(ValueError):
        analyze_episode_coverage(
            ["S02E01"], selected_season=-1, expected_episode_count=None
        )
    with pytest.raises(ValueError):
        analyze_episode_coverage(
            ["S02E01"], selected_season=2, expected_episode_count=-1
        )
