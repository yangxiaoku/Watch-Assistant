import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from watch_assistant.models import Resource, SourceReliability
from watch_assistant.schemas import (
    MediaType,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    SearchResponse,
)
from watch_assistant.services.content_policy import ContentPolicy
from watch_assistant.services.search import (
    MAX_SNAPSHOT_MAGNETS,
    SearchService,
    _dedupe_resources,
    _limit_magnet_resources,
    _quality_matches,
    _resource_facets,
    _resource_sort_key,
    _resource_summary,
    make_cache_key,
    source_penalty,
)


def test_partial_merge_keeps_fresh_resource_over_stale_cache():
    now = datetime.now(UTC)
    fresh = Resource(
        id="res_same",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:same",
        encrypted_url="fresh-cipher",
        name="Movie 2010 2160p",
        seeders=42,
        source="source:fresh",
        captured_at=now,
        expires_at=now,
    )
    cached = Resource(
        id="res_same",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:same",
        encrypted_url="cached-cipher",
        name="Movie 2010 1080p",
        seeders=2,
        source="source:cached",
        captured_at=now - timedelta(days=1),
        expires_at=now,
    )

    assert _dedupe_resources([fresh, cached]) == [fresh]


def test_legacy_search_response_explains_magnet_truncation():
    now = datetime.now(UTC)
    resources = [
        Resource(
            id=f"res_{index}",
            kind=ResourceKind.MAGNET,
            canonical_key=f"magnet:{index:040x}",
            encrypted_url="encrypted",
            name=f"Movie 2010 1080p {index}",
            source="test",
            captured_at=now,
            expires_at=now,
        )
        for index in range(31)
    ]

    response = SearchService._response(
        MovieMetadata(tmdb_id=1, title="Movie", release_year=2010),
        resources,
        now,
        None,
        cached=False,
        policy=ContentPolicy(),
    )

    assert len(response.results) == 30
    assert "resource_results_truncated" in response.warnings


def test_resource_summary_preserves_normalized_source_evidence_with_legacy_fallback():
    now = datetime.now(UTC)
    resource = Resource(
        id="res_sources",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:sources",
        encrypted_url="encrypted",
        name="Movie 2010 1080p",
        source="plugin:prowlarr",
        captured_at=now,
        expires_at=now,
        metadata_json='{"sources":["plugin:pansou", "plugin:prowlarr", "plugin:custom"]}',
    )

    summary = _resource_summary(resource, None)

    assert summary.sources == ["plugin:pansou", "plugin:prowlarr", "plugin:custom"]
    assert summary.source_count == 3

    legacy = Resource(
        id="res_legacy",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:legacy",
        encrypted_url="encrypted",
        name="Legacy",
        source="plugin:prowlarr",
        captured_at=now,
        expires_at=now,
    )
    legacy_summary = _resource_summary(legacy, None)
    assert legacy_summary.sources == ["plugin:prowlarr"]
    assert legacy_summary.source_count == 1


@pytest.mark.asyncio
async def test_query_timeout_does_not_block_other_queries():
    service = SearchService.__new__(SearchService)
    service._pansou_limit = asyncio.Semaphore(2)
    service._pansou_timeout = 0.01
    client = AsyncMock()

    async def search(query: str):
        if query == "slow":
            await asyncio.sleep(1)
        return {"query": query}

    client.search.side_effect = search
    service._pansou = client

    results = await asyncio.gather(
        service._query_pansou("slow"),
        service._query_pansou("fast"),
        return_exceptions=True,
    )

    assert isinstance(results[0], TimeoutError)
    assert results[1] == {"query": "fast"}


@pytest.mark.asyncio
async def test_concurrent_resource_search_receipts_are_idempotent():
    service = SearchService.__new__(SearchService)
    service._resource_search_tasks = {}
    service._resource_search_start_locks = {}
    service._resource_search_finalize_locks = {}
    first_load_started = asyncio.Event()
    release_first_load = asyncio.Event()
    load_calls = 0

    async def load_latest(_key):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            first_load_started.set()
            await release_first_load.wait()

    service._load_latest_resource_search_task = load_latest
    service._snapshot_metadata = AsyncMock(return_value=("revision", 0))
    service._save_resource_search_task = AsyncMock()

    first = asyncio.create_task(
        service.start_resource_search(
            123,
            media_type=MediaType.MOVIE,
            season_number=None,
        )
    )
    await first_load_started.wait()
    second = asyncio.create_task(
        service.start_resource_search(
            123,
            media_type=MediaType.MOVIE,
            season_number=None,
        )
    )
    await asyncio.sleep(0)
    release_first_load.set()

    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.task_id == second_response.task_id
    assert load_calls == 1
    service._save_resource_search_task.assert_awaited_once()


def test_cache_key_is_versioned_by_movie_id():
    assert make_cache_key(12345) == "tmdb:movie:12345:queries:v5"
    assert make_cache_key(12345, MediaType.TV) == "tmdb:tv:12345:queries:v5"
    assert make_cache_key(12345, MediaType.TV, 2) == (
        "tmdb:tv:12345:season:2:queries:v5"
    )


def test_source_penalty_requires_ten_observations():
    immature = SourceReliability(
        source="source:new",
        accepted_count=0,
        rejected_count=9,
        link_ok_count=0,
        link_bad_count=0,
    )
    established = SourceReliability(
        source="source:known",
        accepted_count=1,
        rejected_count=9,
        link_ok_count=0,
        link_bad_count=0,
    )

    assert source_penalty(immature) == 0
    assert source_penalty(established) == 27


def test_snapshot_magnet_limit_keeps_shares_and_quality_tokens_are_bounded():
    captured_at = datetime.now(UTC)
    magnets = [
        NormalizedResource(
            kind=ResourceKind.MAGNET,
            canonical_key=f"magnet:{index:040x}",
            name=f"Movie 2010 1080p x264 {index}",
            url=f"magnet:?xt=urn:btih:{index:040x}",
            source="test",
            captured_at=captured_at,
        )
        for index in range(1, 601)
    ]
    share = NormalizedResource(
        kind=ResourceKind.SHARE,
        canonical_key="share:115.com:test",
        name="Movie share",
        url="https://115.com/s/test",
        source="test",
        captured_at=captured_at,
    )

    limited = _limit_magnet_resources([*magnets, share], 500)

    assert len([item for item in limited if item.kind == ResourceKind.MAGNET]) == 500
    assert share in limited
    assert _quality_matches("Movie 2010 1080p x264", "1080p")
    assert not _quality_matches("Movie 2010 21080p x264", "1080p")
    assert not _quality_matches("Movie 2010 x264", "4k")


def test_partial_snapshot_limit_keeps_fresh_resources_before_cached_ones():
    now = datetime.now(UTC)
    fresh = Resource(
        id="res_fresh",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:fresh",
        encrypted_url="encrypted",
        name="Movie 2010 1080p fresh",
        source="source:fresh",
        captured_at=now,
        expires_at=now,
    )
    cached = [
        Resource(
            id=f"res_cached_{index}",
            kind=ResourceKind.MAGNET,
            canonical_key=f"magnet:cached-{index}",
            encrypted_url="encrypted",
            name=f"Movie 2010 1080p cached-{index}",
            source="source:cached",
            captured_at=now,
            expires_at=now,
        )
        for index in range(500)
    ]

    limited = _limit_magnet_resources(
        _dedupe_resources([fresh, *cached]), MAX_SNAPSHOT_MAGNETS
    )

    assert len(limited) == MAX_SNAPSHOT_MAGNETS
    assert limited[0] is fresh
    assert fresh in limited
    assert cached[-1] not in limited


def test_quality_tags_match_resource_table_tokens_without_false_positives():
    for name, quality in (
        ("Film UHD", "4k"),
        ("Film 2160p", "4k"),
        ("Film SUB", "subtitle"),
        ("Film CHS", "subtitle"),
        ("Film CHT", "subtitle"),
        ("Film 简中", "subtitle"),
        ("Film 繁中", "subtitle"),
        ("Film 双语", "subtitle"),
    ):
        assert _quality_matches(name, quality)
    for name in ("subscribe", "subset", "submarine", "21080p", "720px", "14k"):
        assert not _quality_matches(name, "4k")
        assert not _quality_matches(name, "1080p")
        assert not _quality_matches(name, "720p")
        assert not _quality_matches(name, "subtitle")

    assert _resource_facets(
        [
            Resource(
                id="res_quality",
                kind=ResourceKind.MAGNET,
                canonical_key="magnet:quality",
                encrypted_url="encrypted",
                name="Film UHD CHS",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC),
            )
        ]
    ) == {
        "magnet": 1,
        "share": 0,
        "4k": 1,
        "1080p": 0,
        "720p": 0,
        "subtitle": 1,
    }


def test_resource_sort_options_are_stable_and_use_resource_id_tie_breaker():
    now = datetime.now(UTC)
    first = Resource(
        id="res_a",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:a",
        encrypted_url="encrypted",
        name="Movie 1080p",
        size_bytes=100,
        seeders=2,
        source="test",
        captured_at=now,
        expires_at=now,
    )
    second = Resource(
        id="res_b",
        kind=ResourceKind.MAGNET,
        canonical_key="magnet:b",
        encrypted_url="encrypted",
        name="Movie 720p",
        size_bytes=200,
        seeders=5,
        source="test",
        captured_at=now,
        expires_at=now,
    )
    scores = {
        "res_a": {
            "rank_score": 90,
            "relevance_score": 10,
            "completeness_score": 20,
        },
        "res_b": {
            "rank_score": 80,
            "relevance_score": 30,
            "completeness_score": 40,
        },
    }

    for sort, expected in (
        ("comprehensive", [first, second]),
        ("relevance", [second, first]),
        ("completeness", [second, first]),
        ("size", [second, first]),
        ("seeders", [second, first]),
    ):
        assert (
            sorted(
                (first, second), key=lambda item: _resource_sort_key(item, scores, sort)
            )
            == expected
        )

    tied_scores = {resource_id: {"rank_score": 1} for resource_id in ("res_a", "res_b")}
    tied_first = Resource(
        id=first.id,
        kind=first.kind,
        canonical_key=first.canonical_key,
        encrypted_url=first.encrypted_url,
        name=first.name,
        size_bytes=200,
        seeders=5,
        source=first.source,
        captured_at=first.captured_at,
        expires_at=first.expires_at,
    )
    tied_second = Resource(
        id=second.id,
        kind=second.kind,
        canonical_key=second.canonical_key,
        encrypted_url=second.encrypted_url,
        name=second.name,
        size_bytes=200,
        seeders=5,
        source=second.source,
        captured_at=second.captured_at,
        expires_at=second.expires_at,
    )
    assert sorted(
        (tied_second, tied_first),
        key=lambda item: _resource_sort_key(item, tied_scores, "comprehensive"),
    ) == [tied_first, tied_second]


def test_comprehensive_sort_uses_seeders_size_and_capture_time_before_id():
    now = datetime.now(UTC)
    resources = []
    for resource_id, seeders, size, captured_at in (
        ("res_a", 4, 100, now - timedelta(minutes=2)),
        ("res_b", 8, 100, now - timedelta(minutes=2)),
        ("res_c", 8, 200, now - timedelta(minutes=2)),
        ("res_d", 8, 200, now),
    ):
        resources.append(
            Resource(
                id=resource_id,
                kind=ResourceKind.MAGNET,
                canonical_key=f"magnet:{resource_id}",
                encrypted_url="encrypted",
                name="Movie 1080p",
                size_bytes=size,
                seeders=seeders,
                source="test",
                captured_at=captured_at,
                expires_at=now,
            )
        )
    scores = {
        resource.id: {
            "rank_score": 80,
            "relevance_score": 20,
            "completeness_score": 20,
        }
        for resource in resources
    }
    expected = [resources[3], resources[2], resources[1], resources[0]]

    for sort in ("comprehensive", "relevance", "completeness"):
        assert (
            sorted(resources, key=lambda item: _resource_sort_key(item, scores, sort))
            == expected
        )

    for sort in ("size", "seeders"):
        assert (
            sorted(resources, key=lambda item: _resource_sort_key(item, scores, sort))
            == expected
        )


async def test_warm_media_reports_partial_upstream_as_failure():
    media = MovieMetadata(tmdb_id=1, title="Movie")
    service = SearchService.__new__(SearchService)
    service._search_locks = {}
    service._search_locked = AsyncMock(
        return_value=SearchResponse(
            movie=media,
            results=[],
            warnings=["partial_upstream"],
            cached=False,
        )
    )

    assert await service.warm_media(media) is False
