import pytest

from watch_assistant.services.subtitle_analysis import (
    SubtitleAnalysisError,
    analyze_subtitles,
)


def test_subtitle_analysis_detects_language_flags_pairing_and_name():
    result = analyze_subtitles(
        "Show.S01E02.1080p.mkv",
        ["Show.S01E02.1080p.chs.ass", "Show.S01E03.eng.forced.srt"],
    )

    assert result.items[0].language.value == "zh-Hans"
    assert result.items[0].match_status == "matched"
    assert result.items[0].confidence == "high"
    assert result.items[0].recommended_name == "Show.S01E02.1080p.zh-Hans.ass"
    assert result.items[1].match_status == "conflict"
    assert result.items[1].forced is True
    assert result.items[1].recommended_name is None


def test_subtitle_analysis_keeps_unknown_and_unsupported_explicit():
    result = analyze_subtitles("Movie.mkv", ["Movie.xyz", "Movie.srt"])

    assert result.items[0].match_status == "unsupported"
    assert result.items[1].language.value == "unknown"
    assert "存在不支持的字幕格式" in result.warnings
    assert "有字幕未能从文件名确认语言" in result.warnings


def test_subtitle_analysis_rejects_paths():
    with pytest.raises(SubtitleAnalysisError) as error:
        analyze_subtitles("folder/Movie.mkv", ["Movie.zh.srt"])
    assert error.value.code == "invalid_filename"

