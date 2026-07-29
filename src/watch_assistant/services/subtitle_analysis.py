"""Side-effect-free subtitle filename analysis and pairing."""

from __future__ import annotations

import re
from pathlib import PurePath

from watch_assistant.schemas import (
    SubtitleAnalyzeResponse,
    SubtitleCandidateResponse,
    SubtitleLanguage,
)

SUBTITLE_EXTENSIONS = frozenset(("srt", "ass", "ssa", "sub", "vtt"))
_EPISODE_MARKER = re.compile(r"(?:s\d{1,3}e\d{1,4}|\be\d{1,4})", re.IGNORECASE)
_TOKEN_MARKER = re.compile(
    r"(?:zh[-_. ]?hans|zh[-_. ]?hant|chs|cht|sc|tc|eng|en|zh|cn|繁中|简中|简体|繁体|中文|英文|双语|bilingual|forced|force|sdh|hi)",
    re.IGNORECASE,
)


class SubtitleAnalysisError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def analyze_subtitles(
    video_name: str, subtitle_names: list[str]
) -> SubtitleAnalyzeResponse:
    video_name = _validate_name(video_name)
    video_stem = _stem(video_name)
    video_episode = _episode_marker(video_stem)
    items: list[SubtitleCandidateResponse] = []
    warnings: list[str] = []
    for subtitle_name in subtitle_names:
        name = _validate_name(subtitle_name)
        extension = _extension(name)
        if extension not in SUBTITLE_EXTENSIONS:
            items.append(
                SubtitleCandidateResponse(
                    name=name,
                    extension=extension,
                    language=SubtitleLanguage.UNKNOWN,
                    forced=False,
                    sdh=False,
                    match_status="unsupported",
                    confidence="none",
                )
            )
            warnings.append("存在不支持的字幕格式")
            continue
        stem = _stem(name)
        language = _language(stem)
        forced = _has_token(stem, "forced", "force")
        sdh = _has_token(stem, "sdh", "hi")
        score = _match_score(video_stem, stem, video_episode)
        conflict = video_episode is not None and _episode_marker(stem) not in {
            None,
            video_episode,
        }
        status = "conflict" if conflict else "matched" if score > 0 else "unmatched"
        confidence = (
            "none"
            if status == "conflict"
            else "high"
            if score >= 2
            else "medium"
            if score == 1
            else "low"
        )
        recommended = _recommended_name(video_name, language, forced, extension)
        items.append(
            SubtitleCandidateResponse(
                name=name,
                extension=extension,
                language=language,
                forced=forced,
                sdh=sdh,
                match_status=status,
                confidence=confidence,
                recommended_name=recommended if status == "matched" else None,
            )
        )
        if language == SubtitleLanguage.UNKNOWN:
            warnings.append("有字幕未能从文件名确认语言")
        if status == "conflict":
            warnings.append("有字幕的季集标记与视频不一致")
    return SubtitleAnalyzeResponse(
        video_name=video_name,
        items=items,
        warnings=list(dict.fromkeys(warnings)),
    )


def _validate_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise SubtitleAnalysisError("invalid_filename")
    return PurePath(value.strip()).name


def _extension(value: str) -> str:
    suffix = PurePath(value).suffix.casefold()
    return suffix[1:] if suffix.startswith(".") else ""


def _stem(value: str) -> str:
    suffix = PurePath(value).suffix
    return value[: -len(suffix)] if suffix else value


def _episode_marker(value: str) -> str | None:
    match = _EPISODE_MARKER.search(value.casefold())
    return match.group(0).casefold() if match else None


def _match_score(video_stem: str, subtitle_stem: str, video_episode: str | None) -> int:
    video_normalized = _normalize_stem(video_stem)
    subtitle_normalized = _normalize_stem(subtitle_stem)
    if video_normalized == subtitle_normalized:
        return 2
    if video_normalized and (
        subtitle_normalized.startswith(video_normalized)
        or video_normalized.startswith(subtitle_normalized)
    ):
        return 1
    subtitle_episode = _episode_marker(subtitle_stem)
    if video_episode is not None and subtitle_episode == video_episode:
        return 1
    return 0


def _normalize_stem(value: str) -> str:
    value = _TOKEN_MARKER.sub(" ", value.casefold())
    return " ".join(re.split(r"[. _\-\[\]()]+", value)).strip()


def _language(value: str) -> SubtitleLanguage:
    lowered = value.casefold()
    if any(token in lowered for token in ("双语", "bilingual")):
        return SubtitleLanguage.ZH
    if any(token in lowered for token in ("zh-hans", "zh_hans", "chs", "简中", "简体", "sc")):
        return SubtitleLanguage.ZH_HANS
    if any(token in lowered for token in ("zh-hant", "zh_hant", "cht", "繁中", "繁体", "tc")):
        return SubtitleLanguage.ZH_HANT
    if any(token in lowered for token in ("中文", "zh", "cn")):
        return SubtitleLanguage.ZH
    if any(token in lowered for token in ("英文", "eng", "en")):
        return SubtitleLanguage.EN
    if any(token in lowered for token in ("ja", "jpn", "日语")):
        return SubtitleLanguage.OTHER
    return SubtitleLanguage.UNKNOWN


def _has_token(value: str, *tokens: str) -> bool:
    lowered = value.casefold()
    return any(token.casefold() in lowered for token in tokens)


def _recommended_name(
    video_name: str,
    language: SubtitleLanguage,
    forced: bool,
    extension: str,
) -> str:
    stem = _stem(video_name)
    suffix = f".{language.value}" if language != SubtitleLanguage.UNKNOWN else ".und"
    if forced:
        suffix += ".forced"
    return f"{stem}{suffix}.{extension}"

