"""Deterministic, evidence-only media filename parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

MediaTypeHint = Literal["movie", "tv", "unknown"]
CompanionType = Literal[
    "video",
    "subtitle",
    "sample",
    "trailer",
    "extra",
    "featurette",
    "nfo",
    "poster",
    "unknown",
]

_VIDEO_EXTENSIONS = frozenset(
    {"mkv", "mp4", "avi", "mov", "ts", "m2ts", "wmv", "flv", "webm"}
)
_SUBTITLE_EXTENSIONS = frozenset({"srt", "ass", "ssa", "sub", "vtt"})
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
# Known media suffixes that are legitimately split off a basename.  Anything
# else (e.g. the ".1" in "AAC5.1-WORLD") stays part of the stem.
_KNOWN_MEDIA_EXTENSIONS = frozenset(
    {"nfo"} | _VIDEO_EXTENSIONS | _SUBTITLE_EXTENSIONS | _IMAGE_EXTENSIONS
)

_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<season>[0-9]{1,3})[ ._-]*E(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:(?:S[0-9]{1,3}[ ._-]*E)|(?:E|EP))?[ ._-]*(?P<end>[0-9]{1,4}))?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MULTI_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<season>[0-9]{1,3})X(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>[0-9]{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CHINESE_EPISODE_RE = re.compile(
    # 集号后紧跟 "季" (如 "第1季第2季") 是另一季标记,不是季内集数,
    # 用前瞻排除,避免把多季合集误判为 第1季第2集。
    r"第(?P<season>[0-9]{1,3})季[ ._-]*第(?P<start>[0-9]{1,4})(?!\s*季)"
    r"(?:[ ._-]*(?:-|至|到)[ ._-]*(?P<end>[0-9]{1,4}))?集?",
)
# 无季标记的纯中文集数:"第2集" / "第2-4集" / "第2至5集"。
_CHINESE_EPISODE_ONLY_RE = re.compile(
    r"(?<![0-9])第(?P<start>[0-9]{1,4})(?:[ ._-]*(?:-|~|至|到)[ ._-]*"
    r"(?P<end>[0-9]{1,4}))?集",
)
_SEASON_ONLY_RE = re.compile(
    # 中文分支把 "第" 纳入匹配,掩码时一并遮掉,避免标题残留 "第"。
    r"(?<![A-Za-z0-9])(?:Season[ ._-]*(?P<season>[0-9]{1,3})"
    r"|S(?P<s_season>[0-9]{1,3})|第?(?P<season_cn>[0-9]{1,3})季)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EPISODE_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:Episode|Ep|EP|E|#)[ ._-]*(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>[0-9]{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 年份之后可能出现的第二个季集标记,用于恢复被年份截断的多集范围,
# 例如 "Show.S01E01.2020.E02.mkv" 中的 "E02"。
_SECOND_EPISODE_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:S[0-9]{1,3}[ ._-]*)?(?:E|EP))[ ._-]*"
    r"(?P<number>[0-9]{1,4})(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_RESOLUTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("2160p", re.compile(r"(?<![A-Za-z0-9])2160[Pp](?![A-Za-z0-9])")),
    ("4K", re.compile(r"(?<![A-Za-z0-9])4[Kk](?![A-Za-z0-9])")),
    ("UHD", re.compile(r"(?<![A-Za-z0-9])UHD(?![A-Za-z0-9])", re.IGNORECASE)),
    ("1080p", re.compile(r"(?<![A-Za-z0-9])1080[Pp](?![A-Za-z0-9])")),
    ("720p", re.compile(r"(?<![A-Za-z0-9])720[Pp](?![A-Za-z0-9])")),
)
_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "WEB-DL",
        re.compile(r"(?<![A-Za-z0-9])WEB[ ._-]*DL(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    (
        "WEBRip",
        re.compile(r"(?<![A-Za-z0-9])WEB[ ._-]*Rip(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    (
        "BluRay",
        re.compile(r"(?<![A-Za-z0-9])Blu[ ._-]*Ray(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    ("BDRip", re.compile(r"(?<![A-Za-z0-9])BDRip(?![A-Za-z0-9])", re.IGNORECASE)),
    ("BRRip", re.compile(r"(?<![A-Za-z0-9])BRRip(?![A-Za-z0-9])", re.IGNORECASE)),
    ("HDTV", re.compile(r"(?<![A-Za-z0-9])HDTV(?![A-Za-z0-9])", re.IGNORECASE)),
    ("Remux", re.compile(r"(?<![A-Za-z0-9])Remux(?![A-Za-z0-9])", re.IGNORECASE)),
)
_VIDEO_CODEC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "H.264",
        re.compile(
            r"(?<![A-Za-z0-9])(?:H[ ._-]*264|x264|AVC)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    (
        "H.265",
        re.compile(
            r"(?<![A-Za-z0-9])(?:H[ ._-]*265|x265)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    ("HEVC", re.compile(r"(?<![A-Za-z0-9])HEVC(?![A-Za-z0-9])", re.IGNORECASE)),
    ("AV1", re.compile(r"(?<![A-Za-z0-9])AV1(?![A-Za-z0-9])", re.IGNORECASE)),
)
_HDR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Dolby Vision",
        re.compile(r"(?<![A-Za-z0-9])Dolby[ ._-]*Vision(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    ("HDR10+", re.compile(r"(?<![A-Za-z0-9])HDR10\+(?![A-Za-z0-9])", re.IGNORECASE)),
    ("HDR10", re.compile(r"(?<![A-Za-z0-9])HDR10(?![+A-Za-z0-9])", re.IGNORECASE)),
    ("HDR", re.compile(r"(?<![A-Za-z0-9])HDR(?![A-Za-z0-9])", re.IGNORECASE)),
    ("Dolby Vision", re.compile(r"(?<![A-Za-z0-9])DV(?![A-Za-z0-9])", re.IGNORECASE)),
)
_AUDIO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "TrueHD",
        # The trailing lookahead allows a channel count to follow the codec
        # (e.g. "TrueHD 5.1"), because releases often write it with no separator.
        re.compile(r"(?<![A-Za-z0-9])True[ ._-]*HD(?![A-Za-z])", re.IGNORECASE),
    ),
    (
        "DTS-HD MA",
        re.compile(
            r"(?<![A-Za-z0-9])DTS[ ._-]*HD[ ._-]*MA(?![A-Za-z])", re.IGNORECASE
        ),
    ),
    (
        "E-AC-3",
        re.compile(r"(?<![A-Za-z0-9])E[ ._-]*AC[ ._-]*3(?![A-Za-z])", re.IGNORECASE),
    ),
    ("DTS", re.compile(r"(?<![A-Za-z0-9])DTS(?![A-Za-z])", re.IGNORECASE)),
    ("AC-3", re.compile(r"(?<![A-Za-z0-9])AC[ ._-]*3(?![A-Za-z])", re.IGNORECASE)),
    ("AAC", re.compile(r"(?<![A-Za-z0-9])AAC(?![A-Za-z])", re.IGNORECASE)),
    ("FLAC", re.compile(r"(?<![A-Za-z0-9])FLAC(?![A-Za-z])", re.IGNORECASE)),
    ("Opus", re.compile(r"(?<![A-Za-z0-9])Opus(?![A-Za-z])", re.IGNORECASE)),
)
# Explicit multi-channel markers such as "5.1", "7.1", or "2.0" (optionally with
# a "ch" suffix).  The leading lookahead allows the marker to sit directly after
# an audio codec (e.g. "AAC5.1"), while the dotted/ch shape avoids stealing
# arbitrary digits that belong to a year or part of the title.
_CHANNEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "channel",
        re.compile(
            r"(?<![0-9])[2-7](?:\.[01](?:ch)?|ch)(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
)
_EXPLICIT_SPECIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 允许 SP 后跟 1-2 位编号 ("SP01"/"SP1"/"SP.01" 等动漫特辑命名);
    # 3 位及以上 ("SP500") 拒绝,避免误吞 S&P500 之类的标题词。
    (
        "SP",
        re.compile(
            r"(?<![A-Za-z0-9])SP(?:[ ._-]*[0-9]{1,2})?(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
    ("OVA", re.compile(r"(?<![A-Za-z0-9])OVA(?![A-Za-z0-9])", re.IGNORECASE)),
    ("OAD", re.compile(r"(?<![A-Za-z0-9])OAD(?![A-Za-z0-9])", re.IGNORECASE)),
    ("ONA", re.compile(r"(?<![A-Za-z0-9])ONA(?![A-Za-z0-9])", re.IGNORECASE)),
)
_GENERIC_SPECIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])specials?(?![A-Za-z0-9])", re.IGNORECASE
)
_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Chinese Simplified",
        re.compile(
            r"(?<![A-Za-z0-9])(?:CHS|简中|简体中文)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    (
        "Chinese Traditional",
        re.compile(
            r"(?<![A-Za-z0-9])(?:CHT|繁中|繁體中文)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    ("Chinese", re.compile(r"(?<![A-Za-z0-9])(?:中文|国语|普通话)(?![A-Za-z0-9])")),
    (
        "English",
        re.compile(
            r"(?<![A-Za-z0-9])(?:ENG|EN|English|英文)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    (
        "Japanese",
        re.compile(
            r"(?<![A-Za-z0-9])(?:JPN|JP|Japanese|日语|日文)(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        "Korean",
        re.compile(
            r"(?<![A-Za-z0-9])(?:KOR|KR|Korean|韩语|韩文)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
)
_SUBTITLE_HINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "subtitle",
        re.compile(
            r"(?<![A-Za-z0-9])(?:SUB|SUBS|字幕|中字)(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    ("dual", re.compile(r"(?<![A-Za-z0-9])(?:双语|中英|中日|英中)(?![A-Za-z0-9])")),
    ("forced", re.compile(r"(?<![A-Za-z0-9])forced(?![A-Za-z0-9])", re.IGNORECASE)),
    ("SDH", re.compile(r"(?<![A-Za-z0-9])SDH(?![A-Za-z0-9])", re.IGNORECASE)),
)
_ATMOS_RE = re.compile(r"(?<![A-Za-z0-9])Atmos(?![A-Za-z0-9])", re.IGNORECASE)
_DOLBY_AUDIO_RE = re.compile(
    # The trailing lookahead only blocks letters so channel counts may follow
    # directly (e.g. "DD5.1", "DD+5.1", "DDP5.1"); the remaining "5.1" is
    # masked by _CHANNEL_PATTERNS.
    r"(?<![A-Za-z0-9])(?:Dolby[ ._-]*(?:Audio|Digital)|DD\+?|DDP)(?![A-Za-z])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MediaParseResult:
    """A conservative parse that records evidence without making final decisions."""

    original_filename: str | None = None
    title: str | None = None
    year: int | None = None
    year_candidates: tuple[int, ...] = ()
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    special_hints: tuple[str, ...] = ()
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    hdr: str | None = None
    dolby_vision: bool | None = None
    audio_codec: str | None = None
    atmos: bool | None = None
    dolby_audio: bool | None = None
    language_hints: tuple[str, ...] = ()
    subtitle_hints: tuple[str, ...] = ()
    release_group: str | None = None
    container: str | None = None
    companion_type: CompanionType = "unknown"
    media_type_hint: MediaTypeHint = "unknown"
    evidence: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "MediaParseResult(<redacted>)"

    __str__ = __repr__

    @property
    def title_candidate(self) -> str | None:
        return self.title

    @property
    def cleaned_title(self) -> str | None:
        return self.title

    @property
    def extension(self) -> str | None:
        return self.container

    @property
    def languages(self) -> tuple[str, ...]:
        return self.language_hints

    @property
    def specials(self) -> tuple[str, ...]:
        return self.special_hints


def parse_media_filename(filename: str) -> MediaParseResult:
    """Parse a basename without I/O, network access, or external metadata."""

    if not isinstance(filename, str):
        raise TypeError("filename must be a string")
    text = unicodedata.normalize("NFKC", filename).strip()
    basename = re.split(r"[\\/]", text)[-1]
    stem, extension = _split_extension(basename)
    if not stem:
        return MediaParseResult(original_filename=basename or None, container=extension)

    companion_type = _companion_type(stem, extension)
    year_matches = tuple(_YEAR_RE.finditer(stem))
    year_match = _select_year_match(stem, year_matches)
    year = int(year_match.group()) if year_match else None
    year_candidates = tuple(int(match.group()) for match in year_matches)
    season, episode_start, episode_end, episode_spans, zero_episode = _episode_fields(
        stem
    )
    explicit_special_hints, explicit_special_spans = _matches(
        _EXPLICIT_SPECIAL_PATTERNS, stem
    )
    generic_special_matches = tuple(_GENERIC_SPECIAL_PATTERN.finditer(stem))
    episode_marker_end = max((end for _, end in episode_spans), default=-1)
    post_episode_specials = tuple(
        match
        for match in generic_special_matches
        if episode_spans and match.start() > episode_marker_end
    )
    generic_special_match = (
        post_episode_specials[0] if len(post_episode_specials) == 1 else None
    )
    contextual_special = generic_special_match is not None
    if explicit_special_hints:
        special_hints = explicit_special_hints
        special_spans = explicit_special_spans
    elif zero_episode:
        # E00 (第 0 集) 是特辑占位符:转为特辑提示,不产生集数主张。
        special_hints = ("special",)
        special_spans = ()
    elif contextual_special:
        special_hints = ("special",)
        special_spans = ((generic_special_match.start(), generic_special_match.end()),)
    else:
        special_hints = ()
        special_spans = ()
    resolution, _ = _first_match(_RESOLUTION_PATTERNS, stem)
    _, all_resolution_spans = _matches(_RESOLUTION_PATTERNS, stem)
    source, _ = _first_match(_SOURCE_PATTERNS, stem)
    _, all_source_spans = _matches(_SOURCE_PATTERNS, stem)
    video_codec, _ = _first_match(_VIDEO_CODEC_PATTERNS, stem)
    _, all_codec_spans = _matches(_VIDEO_CODEC_PATTERNS, stem)
    hdr, _ = _first_match(_HDR_PATTERNS, stem)
    _, all_hdr_spans = _matches(_HDR_PATTERNS, stem)
    audio_codec, _ = _first_match(_AUDIO_PATTERNS, stem)
    _, all_audio_spans = _matches(_AUDIO_PATTERNS, stem)
    _, all_channel_spans = _matches(_CHANNEL_PATTERNS, stem)
    language_hints, language_spans = _matches(_LANGUAGE_PATTERNS, stem)
    subtitle_hints, subtitle_spans = _matches(_SUBTITLE_HINT_PATTERNS, stem)
    atmos = _has_token(stem, r"Atmos")
    dolby_audio = _DOLBY_AUDIO_RE.search(stem) is not None
    dolby_vision = hdr == "Dolby Vision"
    atmos_spans = tuple(
        (match.start(), match.end()) for match in _ATMOS_RE.finditer(stem)
    )
    dolby_audio_spans = tuple(
        (match.start(), match.end()) for match in _DOLBY_AUDIO_RE.finditer(stem)
    )
    release_group, group_spans = _release_group(stem)
    companion_spans = _companion_spans(stem, extension)
    title_stem = stem
    if episode_spans:
        # TV: everything from the episode/season marker onward is quality,
        # encoding, or release-group metadata — never part of the title.
        title_stem = stem[: min(start for start, _ in episode_spans)]
    title = _title_candidate(
        title_stem,
        year_match,
        episode_spans,
        special_spans,
        all_resolution_spans,
        all_source_spans,
        all_codec_spans,
        all_hdr_spans,
        all_audio_spans,
        all_channel_spans,
        language_spans,
        subtitle_spans,
        atmos_spans,
        dolby_audio_spans,
        group_spans,
        companion_spans,
    )
    media_type_hint: MediaTypeHint
    if episode_start is not None or season is not None or special_hints:
        media_type_hint = "tv"
    elif companion_type == "video" and year is not None:
        media_type_hint = "movie"
    else:
        media_type_hint = "unknown"

    evidence: list[str] = []
    if title:
        evidence.append("title_candidate")
    if year is not None:
        evidence.append("year")
    if season is not None:
        evidence.append("season")
    if episode_start is not None:
        evidence.append("episode")
    if episode_end is not None:
        evidence.append("episode_range")
    if special_hints:
        evidence.append("special")
    elif generic_special_matches:
        evidence.append("generic_special_token")
    if resolution:
        evidence.append("resolution")
    if source:
        evidence.append("source")
    if video_codec:
        evidence.append("video_codec")
    if hdr:
        evidence.append("hdr")
    if audio_codec:
        evidence.append("audio_codec")
    if atmos is True:
        evidence.append("atmos")
    if dolby_audio is True:
        evidence.append("dolby_audio")
    if language_hints:
        evidence.append("language")
    if subtitle_hints or companion_type == "subtitle":
        evidence.append("subtitle")
    if release_group:
        evidence.append("release_group")
    if companion_type != "unknown":
        evidence.append(f"companion:{companion_type}")
    if media_type_hint != "unknown":
        evidence.append(f"media_type:{media_type_hint}")

    return MediaParseResult(
        original_filename=basename or None,
        title=title,
        year=year,
        year_candidates=year_candidates,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        special_hints=special_hints,
        resolution=resolution,
        source=source,
        video_codec=video_codec,
        hdr=hdr,
        dolby_vision=dolby_vision if dolby_vision else None,
        audio_codec=audio_codec,
        atmos=atmos if atmos else None,
        dolby_audio=dolby_audio if dolby_audio else None,
        language_hints=language_hints,
        subtitle_hints=subtitle_hints,
        release_group=release_group,
        container=extension,
        companion_type=companion_type,
        media_type_hint=media_type_hint,
        evidence=tuple(evidence),
    )


parse_filename = parse_media_filename


def _split_extension(basename: str) -> tuple[str, str | None]:
    """Split a known media extension from a basename.

    A trailing ``.<suffix>`` is only treated as an extension when it is a
    recognised video/subtitle/image container.  Other dotted suffixes (e.g.
    the ``.1`` in ``AAC5.1-WORLD``) stay part of the stem so channel counts
    are not torn across the stem/extension boundary.
    """
    if "." not in basename or basename.endswith("."):
        return basename, None
    stem, extension = basename.rsplit(".", 1)
    folded = extension.casefold()
    if folded in _KNOWN_MEDIA_EXTENSIONS:
        return stem, folded or None
    return basename, None


def _select_year_match(
    stem: str, candidates: tuple[re.Match[str], ...]
) -> re.Match[str] | None:
    """Choose the final year before the first unambiguous metadata marker."""

    if not candidates:
        return None
    metadata_patterns: tuple[re.Pattern[str], ...] = (
        _SEASON_EPISODE_RE,
        _MULTI_SEASON_EPISODE_RE,
        _CHINESE_EPISODE_RE,
        _SEASON_ONLY_RE,
        _EPISODE_LABEL_RE,
        *(pattern for _, pattern in _EXPLICIT_SPECIAL_PATTERNS),
        *(pattern for _, pattern in _RESOLUTION_PATTERNS),
        *(pattern for _, pattern in _SOURCE_PATTERNS),
        *(pattern for _, pattern in _VIDEO_CODEC_PATTERNS),
        *(pattern for _, pattern in _HDR_PATTERNS),
        *(pattern for _, pattern in _AUDIO_PATTERNS),
        *(pattern for _, pattern in _LANGUAGE_PATTERNS),
        *(pattern for _, pattern in _SUBTITLE_HINT_PATTERNS),
    )
    metadata_starts = [
        match.start()
        for pattern in metadata_patterns
        if (match := pattern.search(stem)) is not None
    ]
    cutoff = min(metadata_starts, default=len(stem))
    before_metadata = [
        candidate for candidate in candidates if candidate.start() < cutoff
    ]
    return (before_metadata or list(candidates))[-1]


def _companion_type(stem: str, extension: str | None) -> CompanionType:
    if extension in _SUBTITLE_EXTENSIONS:
        return "subtitle"
    if extension == "nfo":
        return "nfo"
    normalized = stem.casefold()
    if _has_token(normalized, r"trailers?|preview"):
        return "trailer"
    if _has_token(normalized, r"samples?|proof"):
        return "sample"
    if _has_token(normalized, r"featurettes?"):
        return "featurette"
    if _has_token(normalized, r"extras?"):
        return "extra"
    if extension in _IMAGE_EXTENSIONS and _has_token(
        normalized, r"poster|fanart|backdrop|cover"
    ):
        return "poster"
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def _plausible_episode_end(start: int | None, end: int | None) -> bool:
    """一个发布文件不可能跨越超过 200 集;超出即视为 end 组误吞了年份。

    例如 "Show.S01E01.2019.mkv" 中 2019 会被 end 组匹配,若不拒绝,
    自动匹配将永久失败 (EPISODE_OUT_OF_RANGE)。
    """
    return start is not None and end is not None and start < end <= start + 200


def _recover_range_after_year(
    stem: str, match: re.Match[str], start: int | None
) -> tuple[int, int] | None:
    """恢复年份夹中间的多集形态:end 组误吞年份后,在年份之后找第二个季集标记。

    例如 "Show.S01E01.2020.E02.mkv" 中 end 组先吞下 2020,这里在其后
    识别 "E02" 并把范围恢复为 E01-E02,同时把掩码跨度扩展到第二个标记
    结束,避免其残留。只有被吞的数字是合理年份 (19xx/20xx) 时才恢复,
    防止任意数字意外触发范围合并。
    """
    dropped = _group_int(match, "end")
    if dropped is None or not 1900 <= dropped <= 2099:
        return None
    trailing = stem[match.start("end"):]
    second = _SECOND_EPISODE_MARKER_RE.search(trailing)
    if second is None:
        return None
    recovered = int(second.group("number"))
    if not _plausible_episode_end(start, recovered):
        return None
    return recovered, match.start("end") + second.end()


def _episode_fields(
    stem: str,
) -> tuple[
    int | None, int | None, int | None, tuple[tuple[int, int], ...], bool
]:
    """提取季集字段。

    返回值末尾的布尔值表示是否命中 E00 (第 0 集) 特辑占位符——
    此时集号不是有效主张,由调用方转为特辑提示。
    """
    patterns = (_SEASON_EPISODE_RE, _MULTI_SEASON_EPISODE_RE, _CHINESE_EPISODE_RE)
    for pattern in patterns:
        match = pattern.search(stem)
        if match:
            season = _group_int(match, "season")
            start = _group_int(match, "start")
            end = _group_int(match, "end")
            span_end = match.end()
            if start == 0:
                # E00 是特辑占位符:下游契约 (库存/完整性矩阵) 要求集数
                # >= 1,且 TMDB 集号从 1 开始,因此不产生集数主张。
                if end is not None and not _plausible_episode_end(start, end):
                    span_end = match.start("end")
                return season, None, None, ((match.start(), span_end),), True
            if end is not None and not _plausible_episode_end(start, end):
                # end 是年份等误匹配:若年份之后还有第二个季集标记
                # (如 "S01E01.2020.E02"),则恢复完整范围;否则丢弃 end,
                # 掩码收缩到 end 组之前,让年份仍可被 _YEAR_RE 提取。
                recovered = _recover_range_after_year(stem, match, start)
                if recovered is None:
                    end = None
                    span_end = match.start("end")
                else:
                    end, span_end = recovered
            return season, start, end, ((match.start(), span_end),), False
    match = _EPISODE_LABEL_RE.search(stem) or _CHINESE_EPISODE_ONLY_RE.search(stem)
    if match:
        season_match = _SEASON_ONLY_RE.search(stem)
        start = _group_int(match, "start")
        end = _group_int(match, "end")
        span_end = match.end()
        if start == 0:
            # 无季形态的 E00 (如 "Show.E00.mkv") 同样视为特辑占位符。
            if end is not None and not _plausible_episode_end(start, end):
                span_end = match.start("end")
            return None, None, None, ((match.start(), span_end),), True
        if end is not None and not _plausible_episode_end(start, end):
            recovered = _recover_range_after_year(stem, match, start)
            if recovered is None:
                end = None
                span_end = match.start("end")
            else:
                end, span_end = recovered
        spans = [(match.start(), span_end)]
        season = None
        if season_match:
            season = (
                _group_int(season_match, "season")
                or _group_int(season_match, "s_season")
                or _group_int(season_match, "season_cn")
            )
            spans.append((season_match.start(), season_match.end()))
        return (
            season,
            start,
            end,
            tuple(spans),
            False,
        )
    season_match = _SEASON_ONLY_RE.search(stem)
    if season_match:
        season = (
            _group_int(season_match, "season")
            or _group_int(season_match, "s_season")
            or _group_int(season_match, "season_cn")
        )
        return season, None, None, ((season_match.start(), season_match.end()),), False
    return None, None, None, (), False


def _group_int(match: re.Match[str], name: str) -> int | None:
    value = match.groupdict().get(name)
    return int(value) if value else None


def _matches(
    patterns: tuple[tuple[str, re.Pattern[str]], ...], text: str
) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    matches: list[tuple[int, str, tuple[int, int]]] = []
    for value, pattern in patterns:
        match = pattern.search(text)
        if match:
            span = (match.start(), match.end())
            matches.append((match.start(), value, span))
    matches.sort(key=lambda item: item[0])
    return (
        tuple(item[1] for item in matches),
        tuple(item[2] for item in matches),
    )


def _first_match(
    patterns: tuple[tuple[str, re.Pattern[str]], ...], text: str
) -> tuple[str | None, tuple[tuple[int, int], ...]]:
    for value, pattern in patterns:
        match = pattern.search(text)
        if match:
            return value, ((match.start(), match.end()),)
    return None, ()


def _has_token(text: str, pattern: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9])(?:{pattern})(?![A-Za-z0-9])", text, re.IGNORECASE)
        is not None
    )


def _companion_spans(stem: str, extension: str | None) -> tuple[tuple[int, int], ...]:
    patterns = (
        r"trailers?|preview",
        r"samples?|proof",
        r"featurettes?",
        r"extras?",
    )
    if extension in _IMAGE_EXTENSIONS:
        patterns = (*patterns, r"poster|fanart|backdrop|cover")
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend(
            (match.start(), match.end())
            for match in re.finditer(
                rf"(?<![A-Za-z0-9])(?:{pattern})(?![A-Za-z0-9])",
                stem,
                re.IGNORECASE,
            )
        )
    return tuple(spans)


_RELEASE_GROUP_HYPHEN = re.compile(r"-([A-Za-z0-9]{2,40})\s*$")


def _release_group(stem: str) -> tuple[str | None, tuple[tuple[int, int], ...]]:
    match = re.match(r"\s*\[([^\]]{1,40})\]", stem)
    if match is None:
        match = re.search(r"\[([^\]]{1,40})\]\s*$", stem)
    if match:
        candidate = match.group(1).strip()
        if candidate and not _looks_like_technical(candidate):
            return candidate, ((match.start(), match.end()),)
    # Trailing hyphen release groups (e.g. "...AAC5.1-WORLD" -> WORLD).  Only
    # considered when the file already carries a technical marker so a plain
    # hyphenated title is not misread.  The candidate must not look like a
    # technical/audio/language marker, otherwise tokens such as "-Atmos" or
    # "-CHS" would be swallowed by the group.  A hyphen that belongs inside a
    # technical token itself (e.g. the "-DL" of "WEB-DL") never yields a group.
    if _has_technical_marker(stem):
        hyphen = _RELEASE_GROUP_HYPHEN.search(stem)
        if hyphen is not None:
            candidate = hyphen.group(1)
            if (
                not _looks_like_technical(candidate)
                and not _looks_like_media_marker(candidate)
                and not _hyphen_inside_technical_token(stem, hyphen.start())
            ):
                return candidate, ((hyphen.start(), hyphen.end()),)
    return None, ()


def _hyphen_inside_technical_token(stem: str, position: int) -> bool:
    """连字符是否位于某个技术标记内部 (如 "WEB-DL" 的 "-DL")。

    "Show.S01E01.2020.WEB-DL.mkv" 的 "-DL" 是 WEB-DL 源标记的一部分,
    不应被尾缀连字符组规则误判为发布组 "DL";而 "WEB-DL-NTb" 中
    "-NTb" 的连字符位于标记之后,仍应识别为发布组。
    """
    for patterns in (
        _RESOLUTION_PATTERNS,
        _SOURCE_PATTERNS,
        _VIDEO_CODEC_PATTERNS,
        _HDR_PATTERNS,
        _AUDIO_PATTERNS,
        _CHANNEL_PATTERNS,
    ):
        for _, pattern in patterns:
            match = pattern.search(stem)
            if match and match.start() <= position < match.end():
                return True
    return False


def _has_technical_marker(stem: str) -> bool:
    return any(
        pattern.search(stem)
        for patterns in (
            _RESOLUTION_PATTERNS,
            _SOURCE_PATTERNS,
            _VIDEO_CODEC_PATTERNS,
            _HDR_PATTERNS,
            _AUDIO_PATTERNS,
            _CHANNEL_PATTERNS,
        )
        for _, pattern in patterns
    )


def _looks_like_media_marker(value: str) -> bool:
    return bool(
        _ATMOS_RE.search(value)
        or _DOLBY_AUDIO_RE.search(value)
        or any(pattern.search(value) for _, pattern in _LANGUAGE_PATTERNS)
        or any(pattern.search(value) for _, pattern in _SUBTITLE_HINT_PATTERNS)
        or any(pattern.search(value) for _, pattern in _AUDIO_PATTERNS)
        or _CHANNEL_PATTERNS[0][1].search(value)
    )


def _looks_like_technical(value: str) -> bool:
    folded = value.casefold()
    return (
        any(pattern.search(value) for _, pattern in _RESOLUTION_PATTERNS)
        or any(pattern.search(value) for _, pattern in _SOURCE_PATTERNS)
        or any(pattern.search(value) for _, pattern in _VIDEO_CODEC_PATTERNS)
        or any(pattern.search(value) for _, pattern in _HDR_PATTERNS)
        or folded in {"sample", "trailer", "movie", "tv"}
    )


def _title_candidate(
    stem: str,
    year_match: re.Match[str] | None,
    *span_groups: tuple[tuple[int, int], ...],
) -> str | None:
    spans: list[tuple[int, int]] = [span for group in span_groups for span in group]
    if year_match:
        spans.append((year_match.start(), year_match.end()))
    masked = list(stem)
    for start, end in spans:
        for index in range(start, min(end, len(masked))):
            masked[index] = " "
    value = "".join(masked)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"[\[\](){}]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    if not value:
        return None
    return value
