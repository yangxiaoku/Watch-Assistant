import pytest

from watch_assistant.services.anime_mapping import (
    AnimeEpisodeKey,
    AnimeMappingStatus,
    AnimeSeasonRange,
    AnimeSpecialKind,
    parse_anime_file_reference,
    resolve_absolute_episode,
)


def _ranges():
    return (
        AnimeSeasonRange(1, 1, 24, "tmdb", "tmdb-seasons-v1"),
        AnimeSeasonRange(2, 25, 12, "manual", "manual-v1"),
    )


def test_parses_standard_and_absolute_multi_episode_references_without_io():
    standard = parse_anime_file_reference("Title - S01E02-E03.mkv")
    assert [(key.season_number, key.episode_number) for key in standard.keys] == [(1, 2), (1, 3)]
    absolute = parse_anime_file_reference("Title - EP25-26.mkv")
    assert [key.absolute_episode for key in absolute.keys] == [25, 26]


def test_absolute_episode_uses_confirmed_ranges_and_explicit_mapping():
    decision = resolve_absolute_episode(25, _ranges())
    assert decision.status == AnimeMappingStatus.MAPPED
    assert decision.keys[0].season_number == 2
    assert decision.keys[0].episode_number == 1

    explicit = resolve_absolute_episode(
        25,
        _ranges(),
        explicit={25: AnimeEpisodeKey(season_number=3, episode_number=1)},
    )
    assert explicit.status == AnimeMappingStatus.MAPPED
    assert (explicit.keys[0].season_number, explicit.keys[0].episode_number) == (3, 1)


def test_ambiguous_or_out_of_range_baselines_require_review():
    overlap = resolve_absolute_episode(
        25,
        _ranges() + (AnimeSeasonRange(3, 25, 10, "source-b", "v2"),),
    )
    assert overlap.status == AnimeMappingStatus.CONFLICT
    missing = resolve_absolute_episode(99, _ranges())
    assert missing.status == AnimeMappingStatus.NEEDS_REVIEW


def test_special_content_never_becomes_season_zero_automatically():
    reference = parse_anime_file_reference("Title - OVA 01.mkv")
    assert reference.special_kind == AnimeSpecialKind.OVA
    assert reference.keys == ()
    assert parse_anime_file_reference("Title - NCOP.mkv").special_kind == AnimeSpecialKind.NCOP


def test_invalid_standard_key_is_rejected():
    with pytest.raises(ValueError):
        AnimeEpisodeKey(season_number=1)
