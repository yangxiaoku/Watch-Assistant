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

_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<season>[0-9]{1,3})[ ._-]*E(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>[0-9]{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MULTI_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<season>[0-9]{1,3})X(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>[0-9]{1,4}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CHINESE_EPISODE_RE = re.compile(
    r"第(?P<season>[0-9]{1,3})季[ ._-]*第(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:-|至|到)[ ._-]*(?P<end>[0-9]{1,4}))?集?",
)
_SEASON_ONLY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:Season[ ._-]*(?P<season>[0-9]{1,3})"
    r"|S(?P<s_season>[0-9]{1,3})|(?P<season_cn>[0-9]{1,3})季)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EPISODE_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:Episode|Ep|EP|E|#)[ ._-]*(?P<start>[0-9]{1,4})"
    r"(?:[ ._-]*(?:E|EP)?(?P<end>[0-9]{1,4}))?(?![A-Za-z0-9])",
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
        re.compile(r"(?<![A-Za-z0-9])True[ ._-]*HD(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    (
        "DTS-HD MA",
        re.compile(
            r"(?<![A-Za-z0-9])DTS[ ._-]*HD[ ._-]*MA(?![A-Za-z0-9])", re.IGNORECASE
        ),
    ),
    (
        "E-AC-3",
        re.compile(r"(?<![A-Za-z0-9])E[ ._-]*AC[ ._-]*3(?![A-Za-z0-9])", re.IGNORECASE),
    ),
    ("DTS", re.compile(r"(?<![A-Za-z0-9])DTS(?![A-Za-z0-9])", re.IGNORECASE)),
    ("AC-3", re.compile(r"(?<![A-Za-z0-9])AC[ ._-]*3(?![A-Za-z0-9])", re.IGNORECASE)),
    ("AAC", re.compile(r"(?<![A-Za-z0-9])AAC(?![A-Za-z0-9])", re.IGNORECASE)),
    ("FLAC", re.compile(r"(?<![A-Za-z0-9])FLAC(?![A-Za-z0-9])", re.IGNORECASE)),
    ("Opus", re.compile(r"(?<![A-Za-z0-9])Opus(?![A-Za-z0-9])", re.IGNORECASE)),
)
_SPECIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SP", re.compile(r"(?<![A-Za-z0-9])SP(?![A-Za-z0-9])", re.IGNORECASE)),
    ("OVA", re.compile(r"(?<![A-Za-z0-9])OVA(?![A-Za-z0-9])", re.IGNORECASE)),
    ("OAD", re.compile(r"(?<![A-Za-z0-9])OAD(?![A-Za-z0-9])", re.IGNORECASE)),
    ("ONA", re.compile(r"(?<![A-Za-z0-9])ONA(?![A-Za-z0-9])", re.IGNORECASE)),
    ("special", re.compile(r"(?<![A-Za-z0-9])specials?(?![A-Za-z0-9])", re.IGNORECASE)),
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
    r"(?<![A-Za-z0-9])(?:Dolby[ ._-]*(?:Audio|Digital)|DD\+?|DDP)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MediaParseResult:
    """A conservative parse that records evidence without making final decisions."""

    original_filename: str | None = None
    title: str | None = None
    year: int | None = None
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
    year_match = _YEAR_RE.search(stem)
    year = int(year_match.group()) if year_match else None
    season, episode_start, episode_end, episode_spans = _episode_fields(stem)
    special_hints, special_spans = _matches(_SPECIAL_PATTERNS, stem)
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
    title = _title_candidate(
        stem,
        year_match,
        episode_spans,
        special_spans,
        all_resolution_spans,
        all_source_spans,
        all_codec_spans,
        all_hdr_spans,
        all_audio_spans,
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
    if "." not in basename or basename.endswith("."):
        return basename, None
    stem, extension = basename.rsplit(".", 1)
    return stem, extension.casefold() or None


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


def _episode_fields(
    stem: str,
) -> tuple[int | None, int | None, int | None, tuple[tuple[int, int], ...]]:
    patterns = (_SEASON_EPISODE_RE, _MULTI_SEASON_EPISODE_RE, _CHINESE_EPISODE_RE)
    for pattern in patterns:
        match = pattern.search(stem)
        if match:
            season = _group_int(match, "season")
            start = _group_int(match, "start")
            end = _group_int(match, "end")
            return season, start, end, ((match.start(), match.end()),)
    match = _EPISODE_LABEL_RE.search(stem)
    if match:
        season_match = _SEASON_ONLY_RE.search(stem)
        spans = [(match.start(), match.end())]
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
            _group_int(match, "start"),
            _group_int(match, "end"),
            tuple(spans),
        )
    season_match = _SEASON_ONLY_RE.search(stem)
    if season_match:
        season = (
            _group_int(season_match, "season")
            or _group_int(season_match, "s_season")
            or _group_int(season_match, "season_cn")
        )
        return season, None, None, ((season_match.start(), season_match.end()),)
    return None, None, None, ()


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


def _release_group(stem: str) -> tuple[str | None, tuple[tuple[int, int], ...]]:
    match = re.match(r"\s*\[([^\]]{1,40})\]", stem)
    if match is None:
        match = re.search(r"\[([^\]]{1,40})\]\s*$", stem)
    if not match:
        return None, ()
    candidate = match.group(1).strip()
    if not candidate or _looks_like_technical(candidate):
        return None, ()
    return candidate, ((match.start(), match.end()),)


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
