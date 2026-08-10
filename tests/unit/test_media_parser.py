from __future__ import annotations

import pytest

from watch_assistant.services.media_parser import parse_media_filename


def test_movie_title_year_and_technical_evidence_are_extracted():
    parsed = parse_media_filename(
        "[GROUP] The.Matrix.1999.2160p.UHD.BluRay.H.265.HEVC.HDR10+.TrueHD.Atmos.CHS.mkv"
    )

    assert parsed.title == "The Matrix"
    assert parsed.year == 1999
    assert parsed.resolution == "2160p"
    assert parsed.source == "BluRay"
    assert parsed.video_codec == "H.265"
    assert parsed.hdr == "HDR10+"
    assert parsed.audio_codec == "TrueHD"
    assert parsed.atmos is True
    assert parsed.language_hints == ("Chinese Simplified",)
    assert parsed.release_group == "GROUP"
    assert parsed.container == "mkv"
    assert parsed.media_type_hint == "movie"
    assert "title_candidate" in parsed.evidence
    assert "media_type:movie" in parsed.evidence


@pytest.mark.parametrize(
    ("name", "title", "year", "candidates"),
    (
        ("1917.2019.1080p.BluRay.mkv", "1917", 2019, (1917, 2019)),
        (
            "2001.A.Space.Odyssey.1968.1080p.mkv",
            "2001 A Space Odyssey",
            1968,
            (2001, 1968),
        ),
        ("1984.1984.1080p.mkv", "1984", 1984, (1984, 1984)),
    ),
)
def test_numeric_titles_keep_title_years_and_select_final_release_year(
    name, title, year, candidates
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.year == year
    assert parsed.year_candidates == candidates


def test_generic_special_word_stays_in_movie_title():
    special = parse_media_filename("The.Special.2024.1080p.mkv")
    delivery = parse_media_filename("Special.Delivery.2022.1080p.mkv")

    assert special.title == "The Special"
    assert special.media_type_hint == "movie"
    assert special.special_hints == ()
    assert "generic_special_token" in special.evidence
    assert delivery.title == "Special Delivery"
    assert delivery.media_type_hint == "movie"
    assert delivery.special_hints == ()


def test_generic_special_gets_contextual_hint_with_season_episode():
    parsed = parse_media_filename("Show.S01E01.Special.1080p.mkv")

    assert parsed.title == "Show"
    assert parsed.special_hints == ("special",)
    assert parsed.media_type_hint == "tv"


@pytest.mark.parametrize(
    ("name", "title", "special_hints"),
    (
        ("Special.Ops.Lioness.S01E01.1080p.mkv", "Special Ops Lioness", ()),
        ("The.Special.S01E01.1080p.mkv", "The Special", ()),
        ("Show.S01E01.Special.1080p.mkv", "Show", ("special",)),
        ("Show.Season.1.Specials.1080p.mkv", "Show", ("special",)),
    ),
)
def test_generic_special_position_controls_hint_and_title(name, title, special_hints):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.special_hints == special_hints
    assert parsed.media_type_hint == "tv"


def test_multiple_generic_special_tokens_choose_only_a_clear_post_episode_candidate():
    before_and_after = parse_media_filename(
        "Special.Special.Show.S01E01.Special.1080p.mkv"
    )
    ambiguous_suffix = parse_media_filename("Show.S01E01.Special.Specials.1080p.mkv")

    assert before_and_after.title == "Special Special Show"
    assert before_and_after.special_hints == ("special",)
    # 剧集标记之后的 token 一律视为技术信息,不进标题。
    assert ambiguous_suffix.title == "Show"
    assert ambiguous_suffix.special_hints == ()


@pytest.mark.parametrize(
    ("name", "season", "episode_start", "episode_end"),
    (
        ("Show.S01E02.mkv", 1, 2, None),
        ("Show.S01E02-E04.mkv", 1, 2, 4),
        ("Show.1x02.mkv", 1, 2, None),
        ("Show.Season 1 Episode 2.mkv", 1, 2, None),
        ("Show.第01季第02-04集.mkv", 1, 2, 4),
    ),
)
def test_season_and_episode_forms(name, season, episode_start, episode_end):
    parsed = parse_media_filename(name)

    assert parsed.season == season
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.media_type_hint == "tv"


@pytest.mark.parametrize(
    ("name", "title", "season", "episode_start", "episode_end", "year"),
    (
        # 年份紧跟季集标记时,不得被 end 组吞成集数范围
        ("Show.S01E01.2019.mkv", "Show", 1, 1, None, 2019),
        ("Show.S01E01.2020.1080p.WEB-DL.x264-GROUP.mkv", "Show", 1, 1, None, 2020),
        ("Show.E05.2019.1080p.mkv", "Show", None, 5, None, 2019),
        ("Show.1X05.2019.mkv", "Show", 1, 5, None, 2019),
        # 跨季/多段季集标记整体掩码,标题不再残留后半段标记
        ("Show.S01E01-S01E05.2019.1080p.WEB-DL.mkv", "Show", 1, 1, 5, 2019),
        ("Show.S01E01.S02E03.2019.mkv", "Show", 1, 1, 3, 2019),
        # 无年份的常规格式不回归
        ("Show.S01E01-E04.mkv", "Show", 1, 1, 4, None),
    ),
)
def test_year_after_episode_marker_is_not_eaten_as_episode_end(
    name, title, season, episode_start, episode_end, year
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.season == season
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.year == year
    assert parsed.media_type_hint == "tv"


def test_special_episode_hints_are_not_final_media_classification():
    parsed = parse_media_filename("Anime - SP OVA 1080p WEB-DL.mkv")

    assert parsed.special_hints == ("SP", "OVA")
    assert parsed.media_type_hint == "tv"
    assert "special" in parsed.evidence


@pytest.mark.parametrize(
    ("token", "expected"),
    (
        ("2160p", "2160p"),
        ("4K", "4K"),
        ("UHD", "UHD"),
        ("1080p", "1080p"),
        ("720P", "720p"),
    ),
)
def test_resolution_tokens_are_bounded(token, expected):
    assert parse_media_filename(f"Movie 2020 {token}.mkv").resolution == expected


@pytest.mark.parametrize("token", ("12160p", "1080px", "14K", "720pp"))
def test_resolution_false_positives_are_rejected(token):
    assert parse_media_filename(f"Movie 2020 {token}.mkv").resolution is None


def test_codec_dynamic_range_audio_and_dolby_hints():
    parsed = parse_media_filename(
        "Show.S01E01.WEB-DL.AV1.DV.E-AC-3.Dolby.Audio.Atmos.ENG.CHS.mkv"
    )

    assert parsed.source == "WEB-DL"
    assert parsed.video_codec == "AV1"
    assert parsed.hdr == "Dolby Vision"
    assert parsed.dolby_vision is True
    assert parsed.audio_codec == "E-AC-3"
    assert parsed.atmos is True
    assert parsed.dolby_audio is True
    assert parsed.language_hints == ("English", "Chinese Simplified")


def test_subtitle_language_hints_and_companion_type():
    parsed = parse_media_filename("电影.2024.中英双语.简中字幕.srt")

    assert parsed.container == "srt"
    assert parsed.companion_type == "subtitle"
    assert parsed.subtitle_hints == ("dual", "subtitle")
    assert parsed.language_hints == ("Chinese Simplified",)
    assert "subtitle" in parsed.evidence


@pytest.mark.parametrize(
    ("name", "companion"),
    (
        ("Movie.sample.mkv", "sample"),
        ("Movie.trailer.mp4", "trailer"),
        ("Movie.featurette.mkv", "featurette"),
        ("Movie.extra.mkv", "extra"),
        ("Movie.nfo", "nfo"),
        ("Movie-poster.jpg", "poster"),
    ),
)
def test_companion_file_types_are_separate_from_video(name, companion):
    assert parse_media_filename(name).companion_type == companion


def test_unknown_and_malformed_names_remain_conservative():
    parsed = parse_media_filename("not a media name")

    assert parsed.media_type_hint == "unknown"
    assert parsed.year is None
    assert parsed.season is None
    assert parsed.episode_start is None
    assert parsed.resolution is None
    assert parsed.source is None


def test_parse_is_idempotent_and_repr_does_not_leak_input_or_title():
    name = "Secret Title 2024 S01E02 1080p WEB-DL.mkv"
    first = parse_media_filename(name)
    second = parse_media_filename(name)

    assert first == second
    assert first.original_filename == name
    assert first.title_candidate == first.cleaned_title == "Secret Title"
    assert first.extension == "mkv"
    assert name not in repr(first)
    assert "Secret Title" not in repr(first)


def test_parser_keeps_only_basename_when_given_a_path():
    parsed = parse_media_filename(r"C:\private\Secret Title 2024.mkv")

    assert parsed.original_filename == "Secret Title 2024.mkv"
    assert "private" not in parsed.original_filename
    assert "private" not in repr(parsed)


def test_invalid_input_error_does_not_include_the_input():
    secret = "private-title-2024.mkv"

    with pytest.raises(TypeError) as error:
        parse_media_filename({"name": secret})  # type: ignore[arg-type]

    assert secret not in str(error.value)


def test_audio_codec_followed_by_channel_count_keeps_title_clean():
    parsed = parse_media_filename(
        "Kraken.2026.1080p.BluRay.x264.AAC5.1-WORLD.mp4"
    )

    assert parsed.title == "Kraken"
    assert parsed.year == 2026
    assert parsed.audio_codec == "AAC"
    assert parsed.release_group == "WORLD"
    assert parsed.media_type_hint == "movie"


def test_channel_count_and_hyphen_group_are_stripped_from_title():
    parsed = parse_media_filename(
        "Dune.2021.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT.mkv"
    )

    assert parsed.title == "Dune"
    assert parsed.year == 2021
    assert parsed.audio_codec == "TrueHD"
    assert parsed.release_group == "FGT"


def test_tv_title_is_truncated_before_the_episode_marker():
    parsed = parse_media_filename(
        "末日地堡.Silo.S02E01.2024.2160p.ATVP.WEB-DL.DDP5.1.Atmos.H265.DV-ZeroTV.mkv"
    )

    assert parsed.title == "末日地堡 Silo"
    assert "ATVP" not in parsed.title
    assert "DDP" not in parsed.title
    assert "5.1" not in parsed.title
    assert parsed.season == 2
    assert parsed.episode_start == 1
    assert parsed.media_type_hint == "tv"
    assert parsed.dolby_audio is True
    assert parsed.release_group == "ZeroTV"


@pytest.mark.parametrize(
    ("name", "title"),
    (
        ("Show.S01E01.1080p.WEB-DL.DDP5.1.H265.mkv", "Show"),
        ("Show.S01E02.1080p.WEB-DL.DD+5.1.H265.mkv", "Show"),
        ("Show.S01E03.1080p.WEB-DL.DD5.1.H265.mkv", "Show"),
        # 电影路径无剧集标记:靠遮罩保证 title 干净
        ("Movie.2024.1080p.WEB-DL.DDP5.1.Atmos.mkv", "Movie"),
        ("Movie.2024.1080p.WEB-DL.DD+5.1.Atmos.mkv", "Movie"),
        ("Movie.2024.1080p.WEB-DL.DD5.1.Atmos.mkv", "Movie"),
    ),
)
def test_dolby_audio_with_channel_count_stays_out_of_title(name, title):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.dolby_audio is True
    assert "DD" not in parsed.title
    assert "5.1" not in parsed.title


def test_plain_series_title_without_technical_tokens_is_unchanged():
    parsed = parse_media_filename("Show.S01E01.mkv")

    assert parsed.title == "Show"
    assert parsed.media_type_hint == "tv"


def test_movie_title_path_is_not_affected_by_tv_truncation():
    parsed = parse_media_filename(
        "Demon.Slayer.Kimetsu.No.Yaiba.2021.1080p.BluRay.Remux.mkv"
    )

    assert parsed.title == "Demon Slayer Kimetsu No Yaiba"
    assert parsed.year == 2021
    assert parsed.source == "BluRay"
    assert parsed.media_type_hint == "movie"
