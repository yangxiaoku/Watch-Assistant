from __future__ import annotations

import pytest

from watch_assistant.services.media_parser import (
    _is_junk_filename,
    parse_media_filename,
)


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
        ("Show.第1季.第2集.mkv", 1, 2, None),
    ),
)
def test_season_and_episode_forms(name, season, episode_start, episode_end):
    parsed = parse_media_filename(name)

    assert parsed.season == season
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.media_type_hint == "tv"


@pytest.mark.parametrize(
    ("name", "title", "episode_start", "episode_end"),
    (
        ("Show.第2集.mkv", "Show", 2, None),
        ("Show.第02集.mkv", "Show", 2, None),
        ("Show.第2-4集.mkv", "Show", 2, 4),
        ("Show.第2至5集.mkv", "Show", 2, 5),
        ("狂飙.第01集.1080p.mkv", "狂飙", 1, None),
    ),
)
def test_unseasoned_chinese_episode_numbers(
    name, title, episode_start, episode_end
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.media_type_hint == "tv"


def test_chinese_multi_season_markers_do_not_claim_a_fake_episode():
    # "第1季第2季" 是两个季节标记,不是 第1季第2集
    parsed = parse_media_filename("权力的游戏.第1季.第2季.mkv")

    assert parsed.season == 1
    assert parsed.episode_start is None
    assert parsed.episode_end is None
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


@pytest.mark.parametrize(
    ("name", "season", "episode_start", "episode_end", "year"),
    (
        # 年份夹在第一个与第二个季集标记之间:应恢复完整范围,
        # 而不是解析成单集并丢失后面的标记
        ("Show.S01E01.2020.E02.mkv", 1, 1, 2, 2020),
        ("Show.S01E05.2021.EP06.mkv", 1, 5, 6, 2021),
        ("Show.S01E01.2019.S01E05.mkv", 1, 1, 5, 2019),
        ("Show.S01E01.2020.1080p.E02.mkv", 1, 1, 2, 2020),
        ("Show.E05.2021.EP06.mkv", None, 5, 6, 2021),
        # 年份之后没有第二个标记时保持原行为:年份不被 end 吞掉
        ("Show.S01E01.2019.mkv", 1, 1, None, 2019),
    ),
)
def test_year_between_episode_markers_recovers_full_range(
    name, season, episode_start, episode_end, year
):
    parsed = parse_media_filename(name)

    assert parsed.title == "Show"
    assert parsed.season == season
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.year == year
    assert parsed.media_type_hint == "tv"


@pytest.mark.parametrize(
    ("name", "season", "title"),
    (
        ("Show.S01E00.mkv", 1, "Show"),
        ("Show.S01E00.2020.1080p.mkv", 1, "Show"),
        ("Show.S00E00.mkv", 0, "Show"),
        ("Show.E00.mkv", None, "Show"),
    ),
)
def test_zero_episode_is_a_special_placeholder_not_an_episode_claim(
    name, season, title
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.season == season
    assert parsed.episode_start is None
    assert parsed.episode_end is None
    assert parsed.special_hints == ("special",)
    assert parsed.media_type_hint == "tv"
    assert "special" in parsed.evidence
    assert "episode" not in parsed.evidence


def test_season_zero_episodes_remain_real_episode_claims():
    # S00E01 是 TMDB 特辑季 (season 0) 的真实集号,保持集数主张
    parsed = parse_media_filename("Show.S00E01.mkv")

    assert parsed.season == 0
    assert parsed.episode_start == 1
    assert parsed.episode_end is None
    assert parsed.special_hints == ()


@pytest.mark.parametrize(
    ("name", "title", "special_hints", "media_type"),
    (
        ("Show.SP01.mkv", "Show", ("SP",), "tv"),
        ("Show.SP1.1080p.mkv", "Show", ("SP",), "tv"),
        ("Show.SP.01.mkv", "Show", ("SP",), "tv"),
        ("One.Piece.SP1.1080p.mkv", "One Piece", ("SP",), "tv"),
        # 3 位编号不是特辑编号:SP500 可能属于 S&P500 之类标题
        ("Show.SP500.mkv", "Show SP500", (), "unknown"),
        ("S&P500.2020.1080p.mkv", "S&P500", (), "movie"),
        ("Anime - SP OVA 1080p WEB-DL.mkv", "Anime", ("SP", "OVA"), "tv"),
    ),
)
def test_numbered_sp_specials_are_detected_without_swallowing_titles(
    name, title, special_hints, media_type
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.special_hints == special_hints
    assert parsed.media_type_hint == media_type


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


@pytest.mark.parametrize(
    "name",
    (
        # 尾缀 "-DL" 属于 WEB-DL 源标记内部,不是发布组
        "Show.S01E01.2020.1080p.WEB-DL.mkv",
        "Show.S01E01-S01E05.2019.1080p.WEB-DL.mkv",
        "더 글로리.S01E01.1080p.NF.WEB-DL.mkv",
        "Show.S01E01.2020.E02.1080p.WEB-DL.mkv",
        # 编码标记自身的连字符 (H-265) 也不应被当作发布组
        "Show.S01E01.H-265.mkv",
    ),
)
def test_hyphen_inside_technical_token_is_not_a_release_group(name):
    parsed = parse_media_filename(name)

    assert parsed.release_group is None


def test_release_group_after_webdl_hyphen_is_still_detected():
    # 常见的 "WEB-DL-NTb" 命名中,连字符位于标记之后,仍是合法发布组
    parsed = parse_media_filename("Show.S01E01.1080p.WEB-DL-NTb.mkv")

    assert parsed.release_group == "NTb"


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


def test_chinese_season_only_marker_never_leaves_the_leading_character():
    # "第1季" 的整体掩码必须包含 "第",否则标题会残留 "狂飙 第"
    parsed = parse_media_filename("狂飙.第1季.2023.1080p.mkv")

    assert parsed.title == "狂飙"
    assert parsed.season == 1
    assert parsed.episode_start is None
    assert parsed.year == 2023
    assert parsed.media_type_hint == "tv"


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


@pytest.mark.parametrize(
    "name",
    (
        "【更多电视剧集下载请访问 www.BPHDTV.com】....MKV",
        "【更多高清剧集下载请访问 www.BPHDTV.com】....mkv",
    ),
)
def test_advertisement_filenames_are_junk(name):
    assert _is_junk_filename(name) is True


@pytest.mark.parametrize(
    "name",
    (
        "Demon Slayer Kimetsu No Yaiba Infinity Castle (2025).mkv",
        "末日地堡.Silo.S02E01.2024.2160p.ATVP.WEB-DL.DDP5.1.Atmos.H265.DV-ZeroTV.mkv",
        "The.Office.2005.1080p.mkv",
        "www.1TamilMV.city - Dune Part Two (2024) English HQ HDRip - 720p - x264 - (AAC 2.0) - 1.2GB - ESub.mkv",
    ),
)
def test_real_media_filenames_are_never_junk(name):
    # 正常影视文件名绝不能因广告判定被误杀;带广告域名但含年份/分辨率等
    # 媒体特征的真实资源同样按正常文件处理。
    assert _is_junk_filename(name) is False


@pytest.mark.parametrize(
    ("name", "title", "year", "media_type"),
    (
        # 真实线上源目录命名的回归:10bit 残留进标题会让 TMDB 搜索空结果
        (
            "Interstellar.2014.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265.mkv",
            "Interstellar",
            2014,
            "movie",
        ),
        (
            "The.Matrix.1999.1080p.8bit.x264.mkv",
            "The Matrix",
            1999,
            "movie",
        ),
        (
            "Movie.2024.2160p.12bit.x265.mkv",
            "Movie",
            2024,
            "movie",
        ),
    ),
)
def test_bit_depth_markers_never_leak_into_title(name, title, year, media_type):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.year == year
    assert parsed.media_type_hint == media_type
    assert "bit" not in parsed.title.casefold()


@pytest.mark.parametrize(
    ("name", "title", "episode_start", "episode_end"),
    (
        # 番组标准命名 [字幕组][作品名][集数][画质]:集号在方括号里
        (
            "[Airota][Sousou no Frieren][29][1080p HEVC-10bit AAC ASS].mkv",
            "Sousou no Frieren",
            29,
            None,
        ),
        (
            "[Airota][Sousou no Frieren][29-38][1080p HEVC-10bit AAC ASS].mkv",
            "Sousou no Frieren",
            29,
            38,
        ),
        (
            "[Kamigami]Sousou no Frieren S01E29 [1080p][HEVC 10bit][ASS].mkv",
            "Sousou no Frieren",
            29,
            None,
        ),
    ),
)
def test_anime_bracket_episode_numbers_are_recognized(
    name, title, episode_start, episode_end
):
    parsed = parse_media_filename(name)

    assert parsed.title == title
    assert parsed.episode_start == episode_start
    assert parsed.episode_end == episode_end
    assert parsed.media_type_hint == "tv"
    assert "10bit" not in parsed.title.casefold()
    assert "ASS" not in parsed.title.upper()


def test_four_digit_bracket_is_a_year_not_an_episode():
    # "[2016]" 是年份方括号,不得误判为集号
    parsed = parse_media_filename("Movie.Name.[2016].mkv")

    assert parsed.title == "Movie Name"
    assert parsed.year == 2016
    assert parsed.episode_start is None
    assert parsed.media_type_hint == "movie"


def test_anime_bracket_episode_marks_file_as_tv_for_matching():
    parsed = parse_media_filename(
        "[Airota][Sousou no Frieren][29][1080p HEVC-10bit AAC ASS].mkv"
    )

    assert parsed.media_type_hint == "tv"
    assert "episode" in parsed.evidence
