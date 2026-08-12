"""Pure helpers for search orchestration.

Split from search.py (phase D): normalized-resource merging, quality
classification, snapshot scoring, and cache-presentation helpers that do
not touch SearchService instance state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from watch_assistant.adapters.tmdb import build_search_queries
from watch_assistant.models import (
    Resource,
    ResourceSearchJob,
    SearchCache,
    SourceReliability,
)
from watch_assistant.schemas import (
    MediaType,
    MovieCollectionResponse,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    ResourceSearchResponse,
    ResourceSummary,
    WorkflowStageStatus,
)
from watch_assistant.services.content_policy import ContentPolicy
from watch_assistant.services.normalize import (
    merge_normalized_resources,
    normalize_source_id,
)

LEGACY_MAGNET_LIMIT = 30

@dataclass(slots=True)
class _ResourceSearchTask:
    task_id: str
    workflow_id: str | None
    tmdb_id: int
    media_type: MediaType
    season_number: int | None
    refresh: bool
    status: str
    created_at: datetime
    updated_at: datetime
    snapshot_revision: str | None = None
    cache_age_seconds: int | None = None
    sources: list[str] = field(default_factory=list)
    selected_season: int | None = None
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None

    def response(self) -> ResourceSearchResponse:
        return ResourceSearchResponse(
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            tmdb_id=self.tmdb_id,
            media_type=self.media_type,
            season_number=self.season_number,
            status=self.status,
            snapshot_revision=self.snapshot_revision,
            cache_age_seconds=self.cache_age_seconds,
            sources=list(self.sources),
            selected_season=self.selected_season,
            warnings=list(self.warnings),
            error_code=self.error_code,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


def _resource_search_stage_status(status: str) -> WorkflowStageStatus:
    if status in {"queued", "running"}:
        return WorkflowStageStatus.RUNNING
    if status == "ready":
        return WorkflowStageStatus.SUCCEEDED
    return WorkflowStageStatus.FAILED


def _resource_search_task_from_row(row: ResourceSearchJob) -> _ResourceSearchTask:
    try:
        sources = json.loads(row.sources_json)
    except (TypeError, json.JSONDecodeError):
        sources = []
    try:
        warnings = json.loads(row.warnings_json)
    except (TypeError, json.JSONDecodeError):
        warnings = []
    return _ResourceSearchTask(
        task_id=row.task_id,
        workflow_id=row.workflow_id,
        tmdb_id=row.tmdb_id,
        media_type=MediaType(row.media_type),
        season_number=row.season_number,
        refresh=bool(row.refresh),
        status=row.status,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        snapshot_revision=row.snapshot_revision,
        cache_age_seconds=row.cache_age_seconds,
        sources=[item for item in sources if isinstance(item, str)]
        if isinstance(sources, list)
        else [],
        selected_season=row.selected_season,
        warnings=[item for item in warnings if isinstance(item, str)]
        if isinstance(warnings, list)
        else [],
        error_code=row.error_code,
    )

def _merge_normalized(
    existing: NormalizedResource,
    candidate: NormalizedResource,
) -> NormalizedResource:
    has_search_queries = (
        "search_queries" in existing.metadata
        or "search_queries" in candidate.metadata
    )
    queries = list(existing.metadata.get("search_queries", []))
    for query in candidate.metadata.get("search_queries", []):
        if query not in queries:
            queries.append(query)
    sources = list(existing.metadata.get("sources", []))
    for source in candidate.metadata.get("sources", []):
        if source not in sources:
            sources.append(source)
    observations = _merge_source_observations(
        existing.metadata.get("source_observations"),
        candidate.metadata.get("source_observations"),
    )
    merged = merge_normalized_resources(existing, candidate)
    if has_search_queries:
        merged.metadata["search_queries"] = queries
    else:
        merged.metadata.pop("search_queries", None)
    merged.metadata["sources"] = sources
    merged.metadata["source_observations"] = observations
    return merged

def _source_observation(resource: NormalizedResource) -> dict[str, str]:
    """Keep provenance to safe source metadata, never request details."""
    return {
        "source": normalize_source_id(resource.source),
        "captured_at": _as_utc(resource.captured_at).isoformat(),
    }

def _merge_source_observations(*values: object) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            source = item.get("source")
            captured_at = item.get("captured_at")
            if not isinstance(source, str) or not isinstance(captured_at, str):
                continue
            normalized = normalize_source_id(source)
            observation = (normalized, captured_at[:64])
            if observation in seen:
                continue
            seen.add(observation)
            merged.append({"source": normalized, "captured_at": observation[1]})
    return merged

def _stored_warnings(cache: SearchCache) -> list[str]:
    try:
        warnings = json.loads(cache.warnings_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(warnings, list):
        return []
    return [item for item in warnings if isinstance(item, str)]

def _resource_summary(
    item: Resource,
    score_snapshot: Mapping[str, Mapping[str, int]] | None,
) -> ResourceSummary:
    metadata = _resource_metadata(item)
    size_source = metadata.get("size_source")
    seeders_source = metadata.get("seeders_source")
    observed_at = metadata.get("seeders_observed_at")
    normalized_source = normalize_source_id(item.source)
    sources: list[str] = []
    raw_sources = metadata.get("sources")
    if isinstance(raw_sources, list):
        for source in raw_sources:
            normalized = normalize_source_id(source)
            if normalized not in sources:
                sources.append(normalized)
    if normalized_source not in sources:
        sources.append(normalized_source)
    return ResourceSummary(
        resource_id=item.id,
        kind=item.kind,
        name=item.name,
        size_bytes=item.size_bytes,
        seeders=item.seeders,
        source=normalized_source,
        sources=sources,
        source_count=len(sources),
        captured_at=_as_utc(item.captured_at),
        size_source=(size_source if size_source in {"pansou", "inspection"} else None),
        seeders_source=seeders_source if seeders_source == "pansou" else None,
        seeders_observed_at=(
            _parse_metadata_datetime(observed_at)
            if seeders_source == "pansou"
            else None
        ),
        rank_score=_snapshot_score(score_snapshot, item.id, "rank_score"),
        relevance_score=_snapshot_score(score_snapshot, item.id, "relevance_score"),
        completeness_score=_snapshot_score(
            score_snapshot, item.id, "completeness_score"
        ),
    )

def _resource_metadata(resource: Resource) -> dict:
    try:
        metadata = json.loads(resource.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}

def _merge_persisted_resource_metadata(
    existing: Resource | None,
    current: NormalizedResource,
) -> dict[str, object]:
    """Keep source history and optional evidence across partial refreshes."""
    existing_metadata = _resource_metadata(existing) if existing is not None else {}
    current_metadata = current.metadata
    merged = {**existing_metadata, **current_metadata}

    source_values: list[object] = [current_metadata.get("sources"), current.source]
    observation_values: list[object] = [
        current_metadata.get("source_observations"),
        [
            {
                "source": current.source,
                "captured_at": _as_utc(current.captured_at).isoformat(),
            }
        ],
    ]
    if existing is not None:
        source_values = [
            existing_metadata.get("sources"),
            existing.source,
            *source_values,
        ]
        observation_values = [
            existing_metadata.get("source_observations"),
            [
                {
                    "source": existing.source,
                    "captured_at": _as_utc(existing.captured_at).isoformat(),
                }
            ],
            *observation_values,
        ]
    merged["sources"] = _merge_safe_source_ids(*source_values)
    merged["source_observations"] = _merge_source_observations(
        *observation_values
    )

    search_queries = _merge_metadata_strings(
        existing_metadata.get("search_queries"),
        current_metadata.get("search_queries"),
    )
    if search_queries:
        merged["search_queries"] = search_queries
    else:
        merged.pop("search_queries", None)
    return merged

def _merge_safe_source_ids(*values: object) -> list[str]:
    sources: list[str] = []
    for value in values:
        candidates = (
            [value]
            if isinstance(value, str)
            else value
            if isinstance(value, list)
            else []
        )
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = normalize_source_id(candidate)
            if normalized not in sources:
                sources.append(normalized)
    return sources

def _merge_metadata_strings(*values: object) -> list[str]:
    merged: list[str] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item not in merged:
                merged.append(item)
    return merged

def _parse_metadata_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return _as_utc(parsed)

def _merge_warnings(current: list[str], additions: list[str]) -> list[str]:
    return list(dict.fromkeys([*current, *additions]))

def _dedupe_resources(resources: list[Resource]) -> list[Resource]:
    deduplicated: dict[str, Resource] = {}
    for resource in resources:
        deduplicated.setdefault(resource.canonical_key, resource)
    return list(deduplicated.values())

def _fallback_queries(
    media: MovieMetadata,
    alternative_titles: tuple[str, ...],
    season_number: int | None = None,
) -> list[str]:
    existing = {item.casefold() for item in build_search_queries(media, season_number)}
    queries: list[str] = []
    for title in alternative_titles:
        normalized = " ".join(title.split())
        if not normalized or normalized.casefold() in existing:
            continue
        existing.add(normalized.casefold())
        queries.append(normalized)
        if len(queries) == 2:
            break
    return queries

def _limit_magnet_resources(
    resources: list[NormalizedResource], limit: int = LEGACY_MAGNET_LIMIT
) -> list[NormalizedResource]:
    magnets = [item for item in resources if item.kind == ResourceKind.MAGNET]
    allowed = {item.canonical_key for item in magnets[:limit]}
    return [
        item
        for item in resources
        if item.kind == ResourceKind.SHARE or item.canonical_key in allowed
    ]

def _resource_facets(resources: list[Resource]) -> dict[str, int]:
    facets = {
        "magnet": 0,
        "share": 0,
        "4k": 0,
        "1080p": 0,
        "720p": 0,
        "subtitle": 0,
    }
    for resource in resources:
        if resource.kind == ResourceKind.MAGNET:
            facets["magnet"] += 1
        elif resource.kind == ResourceKind.SHARE:
            facets["share"] += 1
        for quality in _quality_tags(resource.name):
            facets[quality] += 1
    return facets

_QUALITY_PATTERNS = {
    "4k": re.compile(r"(?<![a-z0-9])(?:4k|2160p|uhd)(?![a-z0-9])"),
    "1080p": re.compile(r"(?<![a-z0-9])1080p(?![a-z0-9])"),
    "720p": re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])"),
    "subtitle": re.compile(
        r"(?<![a-z0-9])(?:sub|subtitles?|chs|cht)(?![a-z0-9])"
        r"|字幕|简中|繁中|双语"
    ),
}

def _quality_tags(name: str) -> frozenset[str]:
    value = name.casefold()
    return frozenset(
        quality
        for quality, pattern in _QUALITY_PATTERNS.items()
        if pattern.search(value) is not None
    )

def _quality_matches(name: str, quality: str) -> bool:
    return quality in _quality_tags(name)

def _visible_media(
    items: list[MovieMetadata], policy: ContentPolicy
) -> list[MovieMetadata]:
    return [item for item in items if policy.media_visible(item.adult)]

def _hidden_reason_counts(
    resources: list[Resource], policy: ContentPolicy
) -> dict[str, int]:
    counts = {"suspicious": 0, "low_quality": 0, "keyword": 0}
    for resource in resources:
        reason = policy.resource_reason(resource.name)
        if reason in counts:
            counts[reason] += 1
    return counts

def _filter_media_collection(
    response: MovieCollectionResponse, policy: ContentPolicy
) -> MovieCollectionResponse:
    items = _visible_media(response.results, policy)
    # TMDB only provides page-local results here; preserve its collection-wide
    # pagination metadata instead of presenting a fabricated global count.
    return response.model_copy(
        update={
            "results": items,
        }
    )

def _resource_sort_key(
    resource: Resource,
    scores: Mapping[str, Mapping[str, int]],
    sort: str,
) -> tuple[object, ...]:
    resource_scores = scores.get(resource.id, {})
    comprehensive = _comprehensive_sort_key(resource, resource_scores)
    if sort == "comprehensive":
        return comprehensive
    if sort == "relevance":
        return (-resource_scores.get("relevance_score", 0), *comprehensive)
    if sort == "completeness":
        return (-resource_scores.get("completeness_score", 0), *comprehensive)
    if sort == "size":
        return (*_optional_descending(resource.size_bytes), *comprehensive)
    if sort == "seeders":
        return (*_optional_descending(resource.seeders), *comprehensive)
    return comprehensive

def _comprehensive_sort_key(
    resource: Resource,
    scores: Mapping[str, int],
) -> tuple[object, ...]:
    return (
        -scores.get("rank_score", 0),
        *_optional_descending(resource.seeders),
        *_optional_descending(resource.size_bytes),
        -_as_utc(resource.captured_at).timestamp(),
        resource.id,
    )

def _optional_descending(value: int | None) -> tuple[int, int]:
    if value is None or value < 0:
        return (1, 0)
    return (0, -value)

def _selected_season(media: MovieMetadata, season_number: int | None) -> int | None:
    return season_number if media.media_type == MediaType.TV else None

def _decode_resource_snapshot(
    value: str,
) -> tuple[list[str], dict[str, dict[str, int]]]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [], {}
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, str)], {}
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
        return [], {}
    resource_ids: list[str] = []
    scores: dict[str, dict[str, int]] = {}
    for item in payload["resources"]:
        if not isinstance(item, dict) or not isinstance(item.get("resource_id"), str):
            continue
        resource_id = item["resource_id"]
        resource_ids.append(resource_id)
        scores[resource_id] = {
            key: item[key]
            for key in ("rank_score", "relevance_score", "completeness_score")
            if isinstance(item.get(key), int) and 0 <= item[key] <= 100
        }
    return resource_ids, scores

def _resource_score_snapshot(
    resources: list[Resource],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for resource in resources:
        values = _resource_metadata_scores(resource)
        result[resource.id] = {
            key: values.get(key, 0)
            for key in ("rank_score", "relevance_score", "completeness_score")
        }
    return result

def _complete_score_snapshot(
    resources: list[Resource],
    snapshot: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    completed: dict[str, dict[str, int]] = {}
    for resource in resources:
        values = dict(snapshot.get(resource.id, {}))
        if len(values) < 3:
            metadata_scores = _resource_metadata_scores(resource)
            for key, value in metadata_scores.items():
                values.setdefault(key, value)
        completed[resource.id] = {
            key: values.get(key, 0)
            for key in ("rank_score", "relevance_score", "completeness_score")
        }
    return completed

def _resource_metadata_scores(resource: Resource) -> dict[str, int]:
    try:
        metadata = json.loads(resource.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    scores = {
        key: metadata[key]
        for key in ("rank_score", "relevance_score", "completeness_score")
        if isinstance(metadata.get(key), int) and 0 <= metadata[key] <= 100
    }
    return scores if len(scores) == 3 else {}

def _snapshot_score(
    snapshot: Mapping[str, Mapping[str, int]] | None,
    resource_id: str,
    key: str,
) -> int:
    value = (snapshot or {}).get(resource_id, {}).get(key, 0)
    return value if isinstance(value, int) and 0 <= value <= 100 else 0

def source_penalty(source: SourceReliability) -> int:
    total = (
        source.accepted_count
        + source.rejected_count
        + source.link_ok_count
        + source.link_bad_count
    )
    if total < 10:
        return 0
    bad = source.rejected_count + source.link_bad_count
    return min(30, round(30 * bad / total))

def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

def _age(value: datetime, now: datetime) -> timedelta:
    return now - _as_utc(value)
