"""Conservative inventory identity and duplicate decisions.

The scanner owns stable 115 object identities.  This module adds a second,
derived identity layer without treating a filename as proof of equivalence.
Callers may supply a trusted TMDB identity or content digest; otherwise the
result remains a review candidate and can never block a push.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from watch_assistant.services.media_parser import (
    MediaParseResult,
    parse_media_filename,
)


class InventoryError(ValueError):
    """Stable inventory validation error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class DuplicateKind(StrEnum):
    EXACT = "exact_duplicate"
    MEDIA = "media_duplicate"
    VERSION = "version_duplicate"
    REVIEW = "review_candidate"


class InventoryDecision(StrEnum):
    NOT_FOUND = "not_found"
    EXACT_DUPLICATE = "exact_duplicate"
    MEDIA_DUPLICATE = "media_duplicate"
    VERSION_DUPLICATE = "version_duplicate"
    NEEDS_REVIEW = "needs_review"
    INDEX_INCOMPLETE = "index_incomplete"


@dataclass(frozen=True, slots=True, repr=False)
class InventoryFile:
    """One file from a complete immutable scan.

    ``object_id`` is the only required identity.  Optional evidence is
    deliberately caller supplied and must already have passed its own
    contract; this layer never reads a path or a remote file.
    """

    object_id: str
    name: str
    size_bytes: int | None = None
    modified_at: datetime | None = None
    tmdb_id: int | None = None
    media_type: Literal["movie", "tv"] | None = None
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    content_digest: str | None = None
    infohash: str | None = None

    def __post_init__(self) -> None:
        if not _safe_identity(self.object_id, max_length=128):
            raise InventoryError("invalid_object_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise InventoryError("invalid_name")
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool) or self.size_bytes < 0
        ):
            raise InventoryError("invalid_size")
        if self.tmdb_id is not None and (
            isinstance(self.tmdb_id, bool) or self.tmdb_id <= 0
        ):
            raise InventoryError("invalid_tmdb_id")
        if self.media_type is not None and self.media_type not in {"movie", "tv"}:
            raise InventoryError("invalid_media_type")
        if self.season is not None and (
            isinstance(self.season, bool) or self.season < 0
        ):
            raise InventoryError("invalid_season")
        if self.episode_start is not None and (
            isinstance(self.episode_start, bool) or self.episode_start < 1
        ):
            raise InventoryError("invalid_episode")
        if self.episode_end is not None and (
            isinstance(self.episode_end, bool)
            or self.episode_end < 1
            or (
                self.episode_start is not None
                and self.episode_end < self.episode_start
            )
        ):
            raise InventoryError("invalid_episode")
        for value, code in (
            (self.content_digest, "invalid_content_digest"),
            (self.infohash, "invalid_infohash"),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InventoryError(code)

    def __repr__(self) -> str:
        return "InventoryFile(object_id=<redacted>, name=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class MediaIdentity:
    key: str
    title: str
    year: int | None
    media_type: Literal["movie", "tv", "unknown"]
    season: int | None
    episode_start: int | None
    episode_end: int | None
    tmdb_id: int | None
    confidence: Literal["trusted", "candidate", "unknown"]
    resolution: str | None
    source: str | None
    video_codec: str | None
    hdr: str | None

    def __repr__(self) -> str:
        return f"MediaIdentity(key=<redacted>, confidence={self.confidence!r})"


@dataclass(frozen=True, slots=True, repr=False)
class InventoryIdentity:
    file: InventoryFile
    parsed: MediaParseResult
    identity: MediaIdentity

    def __repr__(self) -> str:
        return "InventoryIdentity(file=<redacted>, identity=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class DuplicateGroup:
    group_key: str
    kind: DuplicateKind
    identity_key: str
    object_ids: tuple[str, ...]
    confidence: Literal["trusted", "candidate"]

    def __repr__(self) -> str:
        return (
            "DuplicateGroup(group_key=<redacted>, "
            f"kind={self.kind.value!r}, object_count={len(self.object_ids)})"
        )


@dataclass(frozen=True, slots=True)
class InventoryFreshness:
    complete: bool
    captured_at: datetime | None
    age_seconds: int | None
    threshold_seconds: int
    status: FreshnessStatus


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    complete: bool
    captured_at: datetime | None
    files: tuple[InventoryIdentity, ...]
    freshness: InventoryFreshness
    duplicate_groups: tuple[DuplicateGroup, ...]


def build_identity(item: InventoryFile) -> InventoryIdentity:
    parsed = parse_media_filename(item.name)
    title = _normalize_title(parsed.title)
    media_type = item.media_type or _media_type(parsed)
    season = item.season if item.season is not None else parsed.season
    episode_start = (
        item.episode_start if item.episode_start is not None else parsed.episode_start
    )
    episode_end = item.episode_end if item.episode_end is not None else parsed.episode_end
    confidence: Literal["trusted", "candidate", "unknown"]
    if item.tmdb_id is not None:
        confidence = "trusted"
    elif title and parsed.year is not None and media_type != "unknown":
        confidence = "candidate"
    else:
        confidence = "unknown"
    key = _identity_key(
        tmdb_id=item.tmdb_id,
        media_type=media_type,
        title=title,
        year=parsed.year,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
    )
    return InventoryIdentity(
        file=item,
        parsed=parsed,
        identity=MediaIdentity(
            key=key,
            title=title,
            year=parsed.year,
            media_type=media_type,
            season=season,
            episode_start=episode_start,
            episode_end=episode_end,
            tmdb_id=item.tmdb_id,
            confidence=confidence,
            resolution=parsed.resolution,
            source=parsed.source,
            video_codec=parsed.video_codec,
            hdr=parsed.hdr,
        ),
    )


def build_snapshot(
    files: Iterable[InventoryFile],
    *,
    complete: bool,
    captured_at: datetime | None,
    now: datetime | None = None,
    freshness_threshold_seconds: int = 900,
) -> InventorySnapshot:
    if not isinstance(complete, bool):
        raise InventoryError("invalid_complete")
    if (
        isinstance(freshness_threshold_seconds, bool)
        or not isinstance(freshness_threshold_seconds, int)
        or not 60 <= freshness_threshold_seconds <= 86_400
    ):
        raise InventoryError("invalid_freshness_threshold")
    identities = tuple(build_identity(item) for item in files)
    if len({item.file.object_id for item in identities}) != len(identities):
        raise InventoryError("duplicate_object_id")
    freshness = calculate_freshness(
        complete=complete,
        captured_at=captured_at,
        now=now,
        threshold_seconds=freshness_threshold_seconds,
    )
    return InventorySnapshot(
        complete=complete,
        captured_at=captured_at,
        files=identities,
        freshness=freshness,
        duplicate_groups=tuple(find_duplicate_groups(identities)),
    )


def calculate_freshness(
    *,
    complete: bool,
    captured_at: datetime | None,
    now: datetime | None = None,
    threshold_seconds: int = 900,
) -> InventoryFreshness:
    if not isinstance(complete, bool):
        raise InventoryError("invalid_complete")
    if captured_at is not None:
        captured_at = _utc(captured_at)
    now = _utc(now or datetime.now(UTC))
    age_seconds = None if captured_at is None else max(0, int((now - captured_at).total_seconds()))
    if not complete:
        status = FreshnessStatus.INCOMPLETE
    elif age_seconds is None:
        status = FreshnessStatus.UNKNOWN
    elif age_seconds <= threshold_seconds:
        status = FreshnessStatus.FRESH
    else:
        status = FreshnessStatus.STALE
    return InventoryFreshness(
        complete=complete,
        captured_at=captured_at,
        age_seconds=age_seconds,
        threshold_seconds=threshold_seconds,
        status=status,
    )


def find_duplicate_groups(
    identities: Iterable[InventoryIdentity],
) -> list[DuplicateGroup]:
    items = tuple(identities)
    exact: dict[str, list[str]] = {}
    trusted_media: dict[str, list[str]] = {}
    trusted_version: dict[str, list[str]] = {}
    candidate_media: dict[str, list[str]] = {}
    for item in items:
        if item.file.content_digest:
            exact.setdefault(
                "digest:" + _normalize_exact_identity(item.file.content_digest), []
            ).append(item.file.object_id)
        elif item.file.infohash:
            exact.setdefault(
                "infohash:" + _normalize_exact_identity(item.file.infohash), []
            ).append(item.file.object_id)
        key = item.identity.key
        if item.identity.tmdb_id is not None:
            trusted_media.setdefault(key, []).append(item.file.object_id)
            trusted_version.setdefault(_version_identity_key(item), []).append(
                item.file.object_id
            )
        elif item.identity.confidence == "candidate":
            candidate_media.setdefault(key, []).append(item.file.object_id)
    groups: list[DuplicateGroup] = []
    for key, object_ids in sorted(exact.items()):
        if len(object_ids) > 1:
            groups.append(
                DuplicateGroup(
                    group_key=_group_key(DuplicateKind.EXACT, key),
                    kind=DuplicateKind.EXACT,
                    identity_key=key,
                    object_ids=tuple(sorted(object_ids)),
                    confidence="trusted",
                )
            )
    for key, object_ids in sorted(trusted_media.items()):
        if len(object_ids) > 1:
            groups.append(
                DuplicateGroup(
                    group_key=_group_key(DuplicateKind.MEDIA, key),
                    kind=DuplicateKind.MEDIA,
                    identity_key=key,
                    object_ids=tuple(sorted(object_ids)),
                    confidence="trusted",
                )
            )
    for key, object_ids in sorted(trusted_version.items()):
        if len(object_ids) > 1:
            groups.append(
                DuplicateGroup(
                    group_key=_group_key(DuplicateKind.VERSION, key),
                    kind=DuplicateKind.VERSION,
                    identity_key=key,
                    object_ids=tuple(sorted(object_ids)),
                    confidence="trusted",
                )
            )
    for key, object_ids in sorted(candidate_media.items()):
        if len(object_ids) > 1:
            groups.append(
                DuplicateGroup(
                    group_key=_group_key(DuplicateKind.REVIEW, key),
                    kind=DuplicateKind.REVIEW,
                    identity_key=key,
                    object_ids=tuple(sorted(object_ids)),
                    confidence="candidate",
                )
            )
    return groups


def check_inventory(
    snapshot: InventorySnapshot,
    *,
    object_id: str | None = None,
    content_digest: str | None = None,
    infohash: str | None = None,
    tmdb_id: int | None = None,
    media_type: Literal["movie", "tv"] | None = None,
    season: int | None = None,
    episode_start: int | None = None,
    episode_end: int | None = None,
    name: str | None = None,
) -> InventoryDecision:
    if (
        not snapshot.complete
        or snapshot.freshness.complete is not True
        or snapshot.freshness.status is not FreshnessStatus.FRESH
    ):
        return InventoryDecision.INDEX_INCOMPLETE
    if object_id and any(item.file.object_id == object_id for item in snapshot.files):
        return InventoryDecision.EXACT_DUPLICATE
    if content_digest or infohash:
        normalized_digest = (
            _normalize_exact_identity(content_digest) if content_digest else None
        )
        normalized_infohash = (
            _normalize_exact_identity(infohash) if infohash else None
        )
        for item in snapshot.files:
            if (
                normalized_digest is not None
                and item.file.content_digest is not None
                and _normalize_exact_identity(item.file.content_digest)
                == normalized_digest
            ):
                return InventoryDecision.EXACT_DUPLICATE
            if (
                normalized_infohash is not None
                and item.file.infohash is not None
                and _normalize_exact_identity(item.file.infohash) == normalized_infohash
            ):
                return InventoryDecision.EXACT_DUPLICATE
    if tmdb_id is not None:
        matches = [
            item
            for item in snapshot.files
            if item.identity.tmdb_id == tmdb_id
            and (media_type is None or item.identity.media_type == media_type)
            and _episode_scope_matches(
                item,
                media_type=media_type,
                season=season,
                episode_start=episode_start,
                episode_end=episode_end,
            )
        ]
        if matches:
            if name and any(_same_version(item, name) for item in matches):
                return InventoryDecision.VERSION_DUPLICATE
            return InventoryDecision.MEDIA_DUPLICATE
    if name:
        candidate_key = build_identity(
            InventoryFile(object_id="probe", name=name, media_type=media_type)
        ).identity.key
        if any(item.identity.key == candidate_key for item in snapshot.files):
            return InventoryDecision.NEEDS_REVIEW
    return InventoryDecision.NOT_FOUND


def _episode_scope_matches(
    item: InventoryIdentity,
    *,
    media_type: Literal["movie", "tv"] | None,
    season: int | None,
    episode_start: int | None,
    episode_end: int | None,
) -> bool:
    if media_type != "tv":
        return True
    if season is not None and item.identity.season != season:
        return False
    if episode_start is None:
        return True
    candidate_end = episode_end or episode_start
    item_start = item.identity.episode_start
    item_end = item.identity.episode_end or item_start
    if item_start is None or item_end is None:
        return False
    return item_start <= candidate_end and item_end >= episode_start


def _same_version(item: InventoryIdentity, name: str) -> bool:
    parsed = parse_media_filename(name)
    return (
        item.parsed.resolution == parsed.resolution
        and item.parsed.source == parsed.source
        and item.parsed.video_codec == parsed.video_codec
        and item.parsed.hdr == parsed.hdr
    )


def _version_identity_key(item: InventoryIdentity) -> str:
    return ":".join(
        (
            item.identity.key,
            f"resolution={item.identity.resolution or '-'}",
            f"source={item.identity.source or '-'}",
            f"video_codec={item.identity.video_codec or '-'}",
            f"hdr={item.identity.hdr or '-'}",
        )
    )


def _identity_key(
    *,
    tmdb_id: int | None,
    media_type: str,
    title: str | None,
    year: int | None,
    season: int | None,
    episode_start: int | None,
    episode_end: int | None,
) -> str:
    if tmdb_id is not None:
        base = f"tmdb:{media_type}:{tmdb_id}"
    elif title:
        base = f"candidate:{media_type}:{title}:{year or '-'}"
    else:
        return "unknown"
    if media_type == "tv":
        base += f":s{season if season is not None else '-'}"
        if episode_start is not None:
            base += f":e{episode_start}-{episode_end or episode_start}"
    return base


def _group_key(kind: DuplicateKind, identity_key: str) -> str:
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24]
    return f"{kind.value}:{digest}"


def _media_type(parsed: MediaParseResult) -> Literal["movie", "tv", "unknown"]:
    if parsed.media_type_hint in {"movie", "tv"}:
        return parsed.media_type_hint
    return "unknown"


def _normalize_title(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    return normalized or None


def _normalize_exact_identity(value: str) -> str:
    return value.casefold()


def _safe_identity(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_length
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and "://" not in value
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InventoryError("invalid_timestamp")
    return value.astimezone(UTC)


__all__ = [
    "DuplicateGroup",
    "DuplicateKind",
    "FreshnessStatus",
    "InventoryDecision",
    "InventoryError",
    "InventoryFile",
    "InventoryFreshness",
    "InventoryIdentity",
    "InventorySnapshot",
    "build_identity",
    "build_snapshot",
    "calculate_freshness",
    "check_inventory",
    "find_duplicate_groups",
]
