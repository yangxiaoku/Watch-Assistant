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
