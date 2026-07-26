"""Content safety policy normalization and high-confidence matching."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_SEPARATOR = re.compile(r"[\W_]+", re.UNICODE)
_ASCII = re.compile(r"^[\x00-\x7f]+$")

_SUSPICIOUS_PATTERNS = (
    re.compile(r"(?<![a-z0-9])fc2[ _-]*ppv(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])heyzo(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])1[ _-]*pondo(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])caribbeancom(?![a-z0-9])"),
    re.compile(r"一本道|东京热|麻豆传媒|无码流出|成人影片"),
)
_LOW_QUALITY_PATTERNS = (
    re.compile(r"(?<![a-z0-9])camrip(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])hdcam(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])telesync(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])telecine(?![a-z0-9])"),
    re.compile(r"枪版"),
)


def normalize_keyword(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(_SEPARATOR.sub(" ", normalized).split())


def normalize_keywords(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("blocked_keywords must contain strings")
        keyword = normalize_keyword(value)
        if not 2 <= len(keyword) <= 40:
            raise ValueError("blocked_keyword_length")
        if keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    if len(result) > 50:
        raise ValueError("blocked_keyword_count")
    return tuple(result)


@dataclass(frozen=True)
class ContentPolicy:
    hide_adult_media: bool = True
    hide_suspicious_resources: bool = True
    hide_low_quality_resources: bool = True
    blocked_keywords: tuple[str, ...] = ()
    revision: int = 0

    def media_visible(self, adult: object) -> bool:
        return not (self.hide_adult_media and adult is True)

    def resource_reason(self, name: str) -> str | None:
        normalized = normalize_keyword(name)
        if self.hide_suspicious_resources and any(
            pattern.search(normalized) is not None for pattern in _SUSPICIOUS_PATTERNS
        ):
            return "suspicious"
        if self.hide_low_quality_resources and any(
            pattern.search(normalized) is not None for pattern in _LOW_QUALITY_PATTERNS
        ):
            return "low_quality"
        for keyword in self.blocked_keywords:
            if _keyword_matches(keyword, normalized):
                return "keyword"
        return None


def content_policy_from_json(value: str | None, revision: int = 0) -> ContentPolicy:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        keywords = normalize_keywords(payload.get("blocked_keywords", []))
    except (TypeError, ValueError):
        keywords = ()
    try:
        safe_revision = max(0, int(revision))
    except (TypeError, ValueError):
        safe_revision = 0
    return ContentPolicy(
        hide_adult_media=payload.get("hide_adult_media") is not False,
        hide_suspicious_resources=payload.get("hide_suspicious_resources") is not False,
        hide_low_quality_resources=payload.get("hide_low_quality_resources")
        is not False,
        blocked_keywords=keywords,
        revision=safe_revision,
    )


def _keyword_matches(keyword: str, normalized_name: str) -> bool:
    if _ASCII.fullmatch(keyword):
        escaped = re.escape(keyword).replace(r"\ ", r"\s+")
        return (
            re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", normalized_name)
            is not None
        )
    return keyword in normalized_name
