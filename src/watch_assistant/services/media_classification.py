"""Offline classification and naming plans for matched media."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from watch_assistant.schemas import MediaType
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchSource,
    MediaKind,
)
from watch_assistant.services.media_parser import CompanionType, MediaParseResult


class ClassificationKind(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    ANIME = "anime"
    DOCUMENTARY = "documentary"
    VARIETY = "variety"
    CHILDREN = "children"
    CONCERT = "concert"


class RegionClass(StrEnum):
    DOMESTIC = "domestic"
    WESTERN = "western"
    JAPANESE_KOREAN = "japanese_korean"
    OTHER = "other"


class ClassificationStatus(StrEnum):
    PLANNED = "planned"
    REVIEW_REQUIRED = "review_required"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"


_CATEGORY_DIRS = {
    ClassificationKind.MOVIE: "电影",
    ClassificationKind.TV: "剧集",
    ClassificationKind.ANIME: "动画",
    ClassificationKind.DOCUMENTARY: "纪录片",
    ClassificationKind.VARIETY: "综艺",
    ClassificationKind.CHILDREN: "少儿",
    ClassificationKind.CONCERT: "演唱会",
}
_COUNTRY_NAMES = {
    "US": "美国",
    "JP": "日本",
    "CN": "中国",
    "KR": "韩国",
    "GB": "英国",
    "UK": "英国",
    "FR": "法国",
    "DE": "德国",
    "IT": "意大利",
    "ES": "西班牙",
    "RU": "俄罗斯",
    "IN": "印度",
    "TH": "泰国",
    "TW": "台湾",
    "HK": "香港",
    "MO": "澳门",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "NZ": "新西兰",
    "MX": "墨西哥",
    "BR": "巴西",
    "TR": "土耳其",
    "AE": "阿联酋",
    "ID": "印尼",
    "MY": "马来西亚",
    "SG": "新加坡",
    "PH": "菲律宾",
    "VN": "越南",
    "AR": "阿根廷",
    "CL": "智利",
    "SE": "瑞典",
    "NO": "挪威",
    "DK": "丹麦",
    "FI": "芬兰",
    "NL": "荷兰",
    "BE": "比利时",
    "CH": "瑞士",
    "AT": "奥地利",
    "PL": "波兰",
    "CZ": "捷克",
    "GR": "希腊",
    "IL": "以色列",
    "SA": "沙特",
    "EG": "埃及",
    "ZA": "南非",
    "IE": "爱尔兰",
    "IS": "冰岛",
    "LU": "卢森堡",
    "PT": "葡萄牙",
    "UA": "乌克兰",
}
_REGION_CLASS_NAMES = {
    RegionClass.DOMESTIC: "华语",
    RegionClass.WESTERN: "欧美",
    RegionClass.JAPANESE_KOREAN: "日韩",
    RegionClass.OTHER: "其他",
}

# 区域判定用国家集合(与手动整理目录"欧美剧集/华语电影/日韩动画"风格一致)
_REGION_COUNTRY_SETS: dict[RegionClass, frozenset[str]] = {
    RegionClass.DOMESTIC: frozenset(
        {"CN", "CHN", "HK", "HKG", "MO", "MAC", "TW", "TWN", "SG", "SGP"}
    ),
    RegionClass.WESTERN: frozenset(
        {
            "US", "USA", "GB", "UK", "FR", "DE", "IT", "ES", "PT", "IE", "IS",
            "NL", "BE", "CH", "AT", "SE", "NO", "DK", "FI", "PL", "CZ", "GR",
            "CA", "AU", "NZ", "MX", "BR", "AR", "CL", "RU", "UA", "LU", "TR",
        }
    ),
    RegionClass.JAPANESE_KOREAN: frozenset({"JP", "JPN", "KR", "KOR"}),
}
_VIDEO_EXTENSIONS = frozenset(
    {"mkv", "mp4", "avi", "mov", "ts", "m2ts", "wmv", "flv", "webm"}
)
_COMPANION_TYPES = frozenset(
    {"subtitle", "nfo", "poster", "sample", "trailer", "extra", "featurette"}
)
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class NamingRuleConfig:
    """Versioned, deterministic defaults for a naming preview."""

    rule_version: str = "i06-v1"
    library_root: str = ""
    region_enabled: bool = True
    year_grouping_enabled: bool = False
    include_children_category: bool = False
    include_concert_category: bool = False
    animation_as_anime: bool = True
    preserve_technical_tags: bool = True
    movie_directory_template: str = "{title}{year_label} {tmdb_tag}"
    series_directory_template: str = "{title}{year_label} {tmdb_tag}"
    season_directory_template: str = "Season {season}"
    movie_file_template: str = (
        "{title}{year_label} {tmdb_tag}{technical_tags}.{extension}"
    )
    episode_file_template: str = (
        "{title} - S{season:02d}E{episode_start:02d}{episode_range}"
        "{technical_tags}.{extension}"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("invalid rule version")
        if not isinstance(self.library_root, str):
            raise TypeError("invalid library root")
        root = _safe_segment(self.library_root)
        if self.library_root and root != self.library_root:
            raise ValueError("invalid library root")
        if not isinstance(self.region_enabled, bool):
            raise TypeError("invalid region setting")
        if not isinstance(self.year_grouping_enabled, bool):
            raise TypeError("invalid year setting")
        if not isinstance(self.include_children_category, bool):
            raise TypeError("invalid children category setting")
        if not isinstance(self.include_concert_category, bool):
            raise TypeError("invalid concert category setting")
        if not isinstance(self.animation_as_anime, bool):
            raise TypeError("invalid animation setting")
        if not isinstance(self.preserve_technical_tags, bool):
            raise TypeError("invalid technical tag setting")

    @property
    def region_dirs_enabled(self) -> bool:
        return self.region_enabled


@dataclass(frozen=True, slots=True)
class ClassificationOverride:
    """Human-locked classification and/or region, if present."""

    classification: ClassificationKind | None = None
    region: RegionClass | None = None

    def __post_init__(self) -> None:
        if self.classification is not None:
            object.__setattr__(
                self, "classification", ClassificationKind(self.classification)
            )
        if self.region is not None:
            object.__setattr__(self, "region", RegionClass(self.region))


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    classification_source: str
    region_source: str
    selected_country: str | None = None
    reasons: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "ClassificationEvidence(<redacted>)"

    @property
    def kind_source(self) -> str:
        return self.classification_source


@dataclass(frozen=True, slots=True)
class CompanionFile:
    """A companion identified by a stable caller-owned association key."""

    association_key: str
    parsed: MediaParseResult

    def __post_init__(self) -> None:
        if not _safe_association_key(self.association_key):
            raise ValueError("invalid companion association key")
        if not isinstance(self.parsed, MediaParseResult):
            raise TypeError("companion parsed value is invalid")

    def __repr__(self) -> str:
        return "CompanionFile(<redacted>)"


@dataclass(frozen=True, slots=True)
class CompanionMapping:
    association_key: str
    companion_type: CompanionType
    extension: str
    target_name: str
    language_hints: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "CompanionMapping(<redacted>)"


@dataclass(frozen=True, slots=True)
class NamingPlan:
    status: ClassificationStatus
    classification: ClassificationKind | None = None
    region: str | None = None
    target_path: str | None = None
    target_directory: str | None = None
    display_name: str | None = None
    companion_mappings: tuple[CompanionMapping, ...] = ()
    reasons: tuple[str, ...] = ()
    rule_version: str = "i06-v1"
    evidence: ClassificationEvidence | None = None
    technical_tags: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.status is ClassificationStatus.PLANNED

    def __repr__(self) -> str:
        return f"NamingPlan(status={self.status.value!r}, classification={self.classification!r})"


@dataclass(frozen=True, slots=True)
class ClassificationRequest:
    parsed: MediaParseResult
    decision: MatchDecision
    companions: tuple[CompanionFile, ...] = ()
    override: ClassificationOverride | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.parsed, MediaParseResult):
            raise TypeError("parsed value is invalid")
        if not isinstance(self.decision, MatchDecision):
            raise TypeError("match decision is invalid")
        if not all(isinstance(item, CompanionFile) for item in self.companions):
            raise TypeError("companion list is invalid")


def plan_media(
    parsed: MediaParseResult,
    decision: MatchDecision,
    *,
    rules: NamingRuleConfig | None = None,
    companions: Sequence[CompanionFile] = (),
    override: ClassificationOverride | None = None,
    existing_targets: Iterable[str] = (),
) -> NamingPlan:
    """Build a safe relative naming plan without performing any I/O."""

    config = rules or NamingRuleConfig()
    if not isinstance(parsed, MediaParseResult) or not isinstance(
        decision, MatchDecision
    ):
        raise TypeError("invalid classification input")
    request = ClassificationRequest(parsed, decision, tuple(companions), override)
    if not decision.accepted or decision.selected is None:
        return _review_plan(
            config,
            "match_not_accepted",
            *(reason.value for reason in decision.reasons),
        )
    if decision.confidence is not MatchConfidence.HIGH:
        return _review_plan(config, "match_confidence_insufficient")
    if parsed.companion_type != "video":
        return _unsupported_plan(config, "non_primary_companion")
    if parsed.container not in _VIDEO_EXTENSIONS:
        return _unsupported_plan(config, "unsupported_video_extension")

    classification, classification_source, classification_reasons = _classify(
        parsed, decision, config, override
    )
    if (
        override is None
        and parsed.media_type_hint in {"movie", "tv"}
        and parsed.media_type_hint != decision.selected.media_type.value
    ):
        classification = None
        classification_source = "parser_hint"
        classification_reasons.append("parser_media_type_conflict")
    region, region_source, selected_country, region_reasons = _region(
        decision.selected.origin_countries, override
    )
    reasons = [*classification_reasons, *region_reasons]
    if classification is None:
        return _review_plan(
            config,
            *reasons,
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
        )
    if "origin_country_unknown" in region_reasons:
        return _review_plan(
            config,
            *reasons,
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
            classification=classification,
            region=region,
        )
    if "kind_unknown" in classification_reasons:
        return _review_plan(
            config,
            *reasons,
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
            classification=classification,
            region=region,
        )
    if _requires_episode(parsed, decision.selected.media_type):
        return _review_plan(
            config,
            *reasons,
            "episode_context_required",
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
            classification=classification,
            region=region,
        )
    title = _safe_segment(decision.selected.title)
    if not title:
        return _unsupported_plan(config, "empty_candidate_title")
    rendered = _render_target(
        parsed,
        decision.selected,
        classification,
        region,
        title,
        config,
    )
    if rendered is None:
        return _unsupported_plan(config, "invalid_naming_template")
    target_directory, target_name, technical_tags = rendered
    target_path = _join_relative(target_directory, target_name)
    if target_path is None:
        return _unsupported_plan(config, "invalid_target_path")
    normalized_existing = {
        _normalize_target(value) for value in existing_targets if isinstance(value, str)
    }
    if _normalize_target(target_path) in normalized_existing:
        reasons.append("target_conflict")
        return NamingPlan(
            status=ClassificationStatus.CONFLICT,
            classification=classification,
            region=region,
            target_path=target_path,
            target_directory=target_directory,
            display_name=title,
            reasons=tuple(reasons),
            rule_version=config.rule_version,
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
            technical_tags=technical_tags,
        )
    mappings, companion_error = _companion_mappings(target_name, request.companions)
    if companion_error:
        reasons.append(companion_error)
        return NamingPlan(
            status=ClassificationStatus.CONFLICT,
            classification=classification,
            region=region,
            target_path=target_path,
            target_directory=target_directory,
            display_name=title,
            reasons=tuple(reasons),
            rule_version=config.rule_version,
            evidence=ClassificationEvidence(
                classification_source, region_source, selected_country, tuple(reasons)
            ),
            technical_tags=technical_tags,
        )
    status = ClassificationStatus.PLANNED
    if decision.source is MatchSource.MANUAL:
        reasons.append("manual_lock")
    else:
        reasons.append("tmdb_match_accepted")
    evidence = ClassificationEvidence(
        classification_source, region_source, selected_country, tuple(reasons)
    )
    return NamingPlan(
        status=status,
        classification=classification,
        region=region,
        target_path=target_path,
        target_directory=target_directory,
        display_name=title,
        companion_mappings=mappings,
        reasons=tuple(reasons),
        rule_version=config.rule_version,
        evidence=evidence,
        technical_tags=technical_tags,
    )


def plan_batch(
    requests: Sequence[ClassificationRequest | tuple[MediaParseResult, MatchDecision]],
    *,
    rules: NamingRuleConfig | None = None,
) -> tuple[NamingPlan, ...]:
    """Plan a batch and mark duplicate target paths as conflicts."""

    config = rules or NamingRuleConfig()
    normalized: list[ClassificationRequest] = []
    for item in requests:
        if isinstance(item, ClassificationRequest):
            normalized.append(item)
        elif isinstance(item, tuple) and len(item) == 2:
            normalized.append(ClassificationRequest(item[0], item[1]))
        else:
            raise TypeError("invalid classification request")
    plans = [
        plan_media(
            item.parsed,
            item.decision,
            rules=config,
            companions=item.companions,
            override=item.override,
        )
        for item in normalized
    ]
    by_target: dict[str, list[int]] = {}
    for index, plan in enumerate(plans):
        if plan.status is ClassificationStatus.PLANNED and plan.target_path:
            by_target.setdefault(_normalize_target(plan.target_path), []).append(index)
    for indexes in by_target.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            plan = plans[index]
            reasons = (*plan.reasons, "target_conflict")
            plans[index] = NamingPlan(
                status=ClassificationStatus.CONFLICT,
                classification=plan.classification,
                region=plan.region,
                target_path=plan.target_path,
                target_directory=plan.target_directory,
                display_name=plan.display_name,
                companion_mappings=plan.companion_mappings,
                reasons=reasons,
                rule_version=plan.rule_version,
                evidence=plan.evidence,
                technical_tags=plan.technical_tags,
            )
    return tuple(plans)


build_naming_plan = plan_media
classify_and_plan = plan_media
plan_classification = plan_media
ClassificationPlan = NamingPlan


def _classify(
    parsed: MediaParseResult,
    decision: MatchDecision,
    config: NamingRuleConfig,
    override: ClassificationOverride | None,
) -> tuple[ClassificationKind | None, str, list[str]]:
    selected = decision.selected
    if selected is None:
        return None, "none", ["candidate_missing"]
    if override is not None and override.classification is not None:
        return override.classification, "manual_override", ["manual_classification"]
    kind = selected.kind
    title_signal = " ".join(
        value
        for value in (parsed.original_filename, selected.title)
        if isinstance(value, str)
    )
    if config.include_children_category and _has_category_signal(
        title_signal, ("kids", "children", "child", "cartoon", "\u513f\u7ae5", "\u5c11\u513f")
    ):
        return ClassificationKind.CHILDREN, "filename_signal", ["children_category"]
    if config.include_concert_category and _has_category_signal(
        title_signal, ("concert", "\u6f14\u5531\u4f1a", "\u97f3\u4e50\u4f1a")
    ):
        return ClassificationKind.CONCERT, "filename_signal", ["concert_category"]
    if kind is MediaKind.UNKNOWN:
        if selected.media_type is MediaType.TV:
            return ClassificationKind.TV, "tmdb_media_type", ["kind_unknown"]
        if selected.media_type is MediaType.MOVIE:
            return ClassificationKind.MOVIE, "tmdb_media_type", ["kind_unknown"]
        return None, "tmdb_kind", ["kind_unknown"]
    if kind is MediaKind.ANIME:
        if decision.source is MatchSource.MANUAL:
            return ClassificationKind.ANIME, "manual_lock", ["manual_classification"]
        if config.animation_as_anime:
            return ClassificationKind.ANIME, "tmdb_kind", ["animation_as_anime"]
        return (
            ClassificationKind.TV
            if selected.media_type is MediaType.TV
            else ClassificationKind.MOVIE,
            "tmdb_media_type",
            ["animation_as_anime_disabled"],
        )
    if kind is MediaKind.TV and selected.media_type is not MediaType.TV:
        return None, "tmdb_kind", ["media_type_kind_conflict"]
    if kind is MediaKind.MOVIE and selected.media_type is not MediaType.MOVIE:
        return None, "tmdb_kind", ["media_type_kind_conflict"]
    return ClassificationKind(kind.value), "tmdb_kind", []


def _region(
    countries: Sequence[str], override: ClassificationOverride | None
) -> tuple[str, str, str | None, list[str]]:
    """Resolve the region directory as a Chinese region-class name.

    The region layer groups countries into 华语/欧美/日韩/其他 (matching the
    hand-organized layout 剧集/欧美剧集/...); the first canonical country
    that classifies wins, unmapped or missing countries fall back to "其他".
    """
    if override is not None and override.region is not None:
        return (
            _REGION_CLASS_NAMES[override.region],
            "manual_override",
            None,
            ["manual_region"],
        )
    normalized = _canonical_countries(countries)
    selected: str | None = None
    for country in normalized:
        if country in {"UNKNOWN", "XXX", "ZZ"}:
            continue
        selected = country
        for region, country_set in _REGION_COUNTRY_SETS.items():
            if country in country_set:
                return _REGION_CLASS_NAMES[region], "tmdb_origin_country", country, []
        return (
            _REGION_CLASS_NAMES[RegionClass.OTHER],
            "tmdb_origin_country",
            country,
            ["origin_country_other"],
        )
    return (
        _REGION_CLASS_NAMES[RegionClass.OTHER],
        "tmdb_origin_country",
        selected,
        ["origin_country_unknown"],
    )


def _requires_episode(parsed: MediaParseResult, media_type: MediaType) -> bool:
    return media_type is MediaType.TV and (
        parsed.season is None
        or parsed.episode_start is None
        or parsed.container is None
    )


def _render_target(
    parsed: MediaParseResult,
    candidate: Any,
    classification: ClassificationKind,
    region: str,
    title: str,
    config: NamingRuleConfig,
) -> tuple[str, str, tuple[str, ...]] | None:
    year = candidate.release_year if candidate.release_year is not None else parsed.year
    year_label = f" ({year})" if year is not None else ""
    tmdb_tag = f"{{tmdb-{candidate.tmdb_id}}}"
    technical_tags = _technical_tags(parsed, config.preserve_technical_tags)
    tags_text = f" [{' '.join(technical_tags)}]" if technical_tags else ""
    values = {
        "title": title,
        "year": year or "",
        "year_label": year_label,
        "tmdb_tag": tmdb_tag,
        "technical_tags": tags_text,
        "extension": parsed.container or "",
        "season": parsed.season or 0,
        "episode_start": parsed.episode_start or 0,
        "episode_range": (
            f"-E{parsed.episode_end:02d}"
            if parsed.episode_end is not None
            and parsed.episode_end != parsed.episode_start
            else ""
        ),
    }
    directory_template = (
        config.movie_directory_template
        if candidate.media_type is MediaType.MOVIE
        else config.series_directory_template
    )
    file_template = (
        config.movie_file_template
        if candidate.media_type is MediaType.MOVIE
        else config.episode_file_template
    )
    try:
        directory_name = directory_template.format_map(values)
        file_name = file_template.format_map(values)
        if candidate.media_type is not MediaType.MOVIE:
            season_name = config.season_directory_template.format_map(values)
        else:
            season_name = None
    except (KeyError, ValueError, IndexError):
        return None
    directory_name = _safe_segment(directory_name)
    file_name = _safe_segment(file_name)
    if not directory_name or not file_name:
        return None
    category = _CATEGORY_DIRS[classification]
    # 国家层与类别联动命名:欧美剧集/华语电影/日韩动画...
    # (与手动整理目录 剧集/欧美剧集/... 一致)
    region_part = (
        f"{region}{category}" if config.region_enabled else None
    )
    segments = [
        config.library_root,
        category,
        region_part,
        str(year) if config.year_grouping_enabled and year is not None else None,
        directory_name,
        season_name,
        file_name,
    ]
    safe_segments = [segment for segment in segments if segment]
    if any(_safe_segment(segment) != segment for segment in safe_segments):
        return None
    target_directory = "/".join(safe_segments[:-1])
    return target_directory, safe_segments[-1], technical_tags


def _technical_tags(parsed: MediaParseResult, enabled: bool) -> tuple[str, ...]:
    if not enabled:
        return ()
    values: list[str] = []
    for value in (
        parsed.resolution,
        parsed.source,
        parsed.video_codec,
        parsed.hdr,
        parsed.audio_codec,
        "Atmos" if parsed.atmos else None,
        "Dolby Audio" if parsed.dolby_audio else None,
        parsed.release_group,
    ):
        safe = _safe_segment(value) if value else None
        if safe and safe not in values:
            values.append(safe)
    return tuple(values)


def _companion_mappings(
    target_name: str, companions: Sequence[CompanionFile]
) -> tuple[tuple[CompanionMapping, ...], str | None]:
    base = target_name.rsplit(".", 1)[0]
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    mappings: list[CompanionMapping] = []
    for companion in sorted(companions, key=lambda item: item.association_key):
        if companion.association_key in seen_keys:
            return (), "duplicate_companion_key"
        seen_keys.add(companion.association_key)
        parsed = companion.parsed
        if parsed.companion_type not in _COMPANION_TYPES or not parsed.container:
            return (), "unsupported_companion"
        suffix = parsed.companion_type
        language = _language_suffix(parsed.language_hints)
        suffix = f"{suffix}.{language}" if language else suffix
        target = _safe_segment(f"{base}.{suffix}.{parsed.container}")
        if not target or target in seen_names:
            return (), "companion_target_conflict"
        seen_names.add(target)
        mappings.append(
            CompanionMapping(
                association_key=companion.association_key,
                companion_type=parsed.companion_type,
                extension=parsed.container,
                target_name=target,
                language_hints=parsed.language_hints,
            )
        )
    return tuple(mappings), None


def _language_suffix(values: Sequence[str]) -> str:
    mapping = {
        "Chinese Simplified": "chs",
        "Chinese Traditional": "cht",
        "Chinese": "zh",
        "English": "en",
        "Japanese": "ja",
        "Korean": "ko",
    }
    return "-".join(mapping[value] for value in values if value in mapping)


def _review_plan(
    config: NamingRuleConfig,
    *reasons: str,
    evidence: ClassificationEvidence | None = None,
    classification: ClassificationKind | None = None,
    region: str | None = None,
) -> NamingPlan:
    return NamingPlan(
        status=ClassificationStatus.REVIEW_REQUIRED,
        classification=classification,
        region=region,
        reasons=tuple(reason for reason in reasons if reason),
        rule_version=config.rule_version,
        evidence=evidence,
    )


def _unsupported_plan(config: NamingRuleConfig, *reasons: str) -> NamingPlan:
    return NamingPlan(
        status=ClassificationStatus.UNSUPPORTED,
        reasons=tuple(reasons),
        rule_version=config.rule_version,
    )


def _canonical_countries(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value.strip().upper()
                for value in values
                if isinstance(value, str) and value.strip()
            }
        )
    )


def _has_category_signal(value: str, tokens: Sequence[str]) -> bool:
    normalized = value.casefold()
    return any(
        token.casefold() in normalized
        for token in tokens
        if isinstance(token, str) and token
    )


def _safe_association_key(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not bool(_CONTROL_CHARS.search(value) or "/" in value or "\\" in value)
    )


def _safe_segment(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL_CHARS.sub(" ", normalized)
    normalized = _FORBIDDEN_FILENAME_CHARS.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized or normalized in {".", ".."}:
        return None
    if normalized.upper() in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"
    return normalized


def _normalize_target(value: object) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _join_relative(directory: str, name: str) -> str | None:
    directory_parts = directory.split("/") if directory else []
    parts = [*directory_parts, name]
    if not parts or any(
        not part or part in {".", ".."} or "\\" in part or _safe_segment(part) != part
        for part in parts
    ):
        return None
    return "/".join(parts)
