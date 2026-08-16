import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from watch_assistant.models import (
    Resource,
    ResourceSearchJob,
    SearchCache,
    SourceReliability,
)
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


def test_cache_ttl_is_configurable_and_drives_freshness():
    service = SearchService.__new__(SearchService)
    service._fresh_cache_ttl = timedelta(hours=1)
    service._negative_cache_ttl = timedelta(minutes=5)
    service._partial_cache_ttl = timedelta(minutes=2)

    assert service._cache_ttl("positive") == timedelta(hours=1)
    assert service._cache_ttl("negative") == timedelta(minutes=5)
    assert service._cache_ttl("partial") == timedelta(minutes=2)

    now = datetime.now(UTC)
    fresh_cache = SearchCache(
        cache_key="k",
        resource_ids_json="[]",
        warnings_json="[]",
        cache_kind="positive",
        fetched_at=now - timedelta(minutes=30),
        expires_at=now,
    )
    stale_cache = SearchCache(
        cache_key="k2",
        resource_ids_json="[]",
        warnings_json="[]",
        cache_kind="positive",
        fetched_at=now - timedelta(hours=2),
        expires_at=now,
    )
    # TTL 调成 1 小时后,30 分钟前的快照仍新鲜,2 小时前的不再新鲜。
    assert service._cache_is_fresh(fresh_cache, timedelta(minutes=30))
    assert not service._cache_is_fresh(stale_cache, timedelta(hours=2))


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


def test_comprehensive_sort_prioritizes_full_season_packs_over_single_episodes():
    """剧集详情页:综合排序必须把整季/全剧包排在分卷包之前、单集最后,
    避免高做种数的单集(S05E01 等)挤占列表前列。"""
    now = datetime.now(UTC)
    packs = [
        Resource(
            id="res_full",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:full",
            encrypted_url="encrypted",
            name="Show S01 COMPLETE 2160p WEB-DL",
            seeders=5,
            size_bytes=100,
            source="test",
            captured_at=now,
            expires_at=now,
        ),
        Resource(
            id="res_part",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:part",
            encrypted_url="encrypted",
            name="Show S01 Vol.1 E01-E04 1080p WEB-DL",
            seeders=8000,
            size_bytes=80,
            source="test",
            captured_at=now,
            expires_at=now,
        ),
        Resource(
            id="res_episode",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:episode",
            encrypted_url="encrypted",
            name="Show S01E01 1080p WEB-DL",
            seeders=10000,
            size_bytes=10,
            source="test",
            captured_at=now,
            expires_at=now,
        ),
    ]
    # 单集做种最高、完整度也最高:若不按覆盖层级排序,它会排到最前。
    scores = {
        "res_full": {"rank_score": 70, "relevance_score": 90, "completeness_score": 70},
        "res_part": {"rank_score": 75, "relevance_score": 85, "completeness_score": 75},
        "res_episode": {
            "rank_score": 80,
            "relevance_score": 80,
            "completeness_score": 80,
        },
    }

    for sort in ("comprehensive", "relevance", "completeness"):
        ordered = sorted(
            packs,
            key=lambda item: _resource_sort_key(
                item, scores, sort, media_type=MediaType.TV
            ),
        )
        assert [item.id for item in ordered] == [
            "res_full",
            "res_part",
            "res_episode",
        ], f"sort={sort}"

    # 做种/大小排序保持原始数值排序,不做覆盖层级干预。
    assert [
        item.id
        for item in sorted(
            packs,
            key=lambda item: _resource_sort_key(
                item, scores, "seeders", media_type=MediaType.TV
            ),
        )
    ] == ["res_episode", "res_part", "res_full"]


def test_movie_sort_does_not_demote_part_or_volume_titles():
    """电影名中的 Part II / Vol. 2 不是剧集分卷,不得被覆盖层级降权。"""
    now = datetime.now(UTC)
    movies = [
        Resource(
            id="res_volume",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:volume",
            encrypted_url="encrypted",
            name="The Godfather Part II 1974 2160p",
            seeders=9000,
            size_bytes=100,
            source="test",
            captured_at=now,
            expires_at=now,
        ),
        Resource(
            id="res_plain",
            kind=ResourceKind.MAGNET,
            canonical_key="magnet:plain",
            encrypted_url="encrypted",
            name="The Godfather 2 1974 1080p",
            seeders=5,
            size_bytes=10,
            source="test",
            captured_at=now,
            expires_at=now,
        ),
    ]
    scores = {
        "res_volume": {"rank_score": 80, "relevance_score": 80, "completeness_score": 80},
        "res_plain": {"rank_score": 70, "relevance_score": 70, "completeness_score": 70},
    }
    ordered = sorted(
        movies,
        key=lambda item: _resource_sort_key(
            item, scores, "comprehensive", media_type=MediaType.MOVIE
        ),
    )
    assert [item.id for item in ordered] == ["res_volume", "res_plain"]


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


@pytest.mark.asyncio
async def test_fresh_negative_cache_snapshot_stabilizes_search_task():
    """A fresh negative (empty) cache stabilizes the search task as ready.

    REQ-005: empty results use a short negative cache (30m TTL); within that
    window the frontend must show the "no resources" empty state instead of
    polling forever. Regression: an empty negative cache returned no snapshot
    revision, so every poll degraded the ready task back to queued and
    re-triggered the search -- an infinite poll loop.
    """
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import SearchCache

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                SearchCache(
                    cache_key="tmdb:movie:123:queries:v5",
                    resource_ids_json="{}",
                    warnings_json="[]",
                    cache_kind="negative",
                    fetched_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(minutes=25),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        revision, age = await service._snapshot_metadata(123, MediaType.MOVIE, None)
        assert revision is not None
        assert age == 300
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_stale_negative_cache_snapshot_does_not_block_re_search():
    """A stale negative cache must not stabilize: the empty TTL elapsed, so a
    real re-search is required (REQ-005 empty-result auto re-check)."""
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import SearchCache

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                SearchCache(
                    cache_key="tmdb:movie:123:queries:v5",
                    resource_ids_json="{}",
                    warnings_json="[]",
                    cache_kind="negative",
                    fetched_at=now - timedelta(minutes=31),
                    expires_at=now + timedelta(minutes=25),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        revision, age = await service._snapshot_metadata(123, MediaType.MOVIE, None)
        assert revision is None
        assert age is None
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_partial_cache_snapshot_stabilizes_search_task():
    """A partial cache still carries resources, so it must yield a non-None
    snapshot revision and let the search task reach ready instead of polling
    forever (regression: partial was treated like negative and rejected).
    """
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import SearchCache

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                SearchCache(
                    cache_key="tmdb:movie:123:queries:v5",
                    resource_ids_json=json.dumps(
                        {"resources": [{"resource_id": "res_a"}]}
                    ),
                    warnings_json='["partial_upstream"]',
                    cache_kind="partial",
                    fetched_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        revision, age = await service._snapshot_metadata(123, MediaType.MOVIE, None)
        assert revision is not None
        assert age == 300
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_fresh_empty_partial_cache_snapshot_stabilizes_search_task():
    """A fresh empty partial snapshot must still stabilize the task as ready.

    When every candidate source fails and zero resources match, the persisted
    partial snapshot holds an empty resource list. Rejecting it forces every
    poll to degrade the ready task back to queued and re-run the search --
    an infinite poll loop (regression observed in production: 893 completed
    events in one morning, frontend spinning forever). The 10-minute partial
    TTL bounds the "search still unfinished" window; after expiry the task
    re-searches (see test_stale_empty_partial_cache_snapshot_does_not_stabilize).
    """
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import SearchCache

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                SearchCache(
                    cache_key="tmdb:movie:123:queries:v5",
                    resource_ids_json=json.dumps({"resources": []}),
                    warnings_json='["partial_upstream"]',
                    cache_kind="partial",
                    fetched_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        revision, age = await service._snapshot_metadata(123, MediaType.MOVIE, None)
        assert revision is not None
        assert age == 300
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_stale_empty_partial_cache_snapshot_does_not_stabilize():
    """A stale empty partial snapshot must not stabilize: its 10-minute window
    elapsed, so the task must re-search instead of keeping the empty state."""
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import SearchCache

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                SearchCache(
                    cache_key="tmdb:movie:123:queries:v5",
                    resource_ids_json=json.dumps({"resources": []}),
                    warnings_json='["partial_upstream"]',
                    cache_kind="partial",
                    fetched_at=now - timedelta(minutes=11),
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        revision, age = await service._snapshot_metadata(123, MediaType.MOVIE, None)
        assert revision is None
        assert age is None
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_stale_ready_task_without_cache_is_requeued():
    """Regression: ready 任务在 SearchCache 过期/缺失后不能继续返回 ready。

    之前 start_resource_search 会把旧的 ready 任务直接返回，前端随后请求
    resources 得到 404；现在必须重新排队触发搜索。
    """
    now = datetime.now(UTC)
    from watch_assistant.db import create_database, initialize_database

    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                ResourceSearchJob(
                    task_id="resource_search_stale",
                    workflow_id=None,
                    tmdb_id=123,
                    media_type=MediaType.MOVIE,
                    season_number=None,
                    refresh=False,
                    status="ready",
                    snapshot_revision="2026-08-09T00:00:00+00:00",
                    query_plan_version="v5",
                    cache_age_seconds=0,
                    sources_json="[]",
                    selected_season=None,
                    warnings_json="[]",
                    error_code=None,
                    created_at=now - timedelta(days=6),
                    updated_at=now - timedelta(days=6),
                )
            )
            await session.commit()

        from watch_assistant.services.search import SearchService

        service = SearchService(
            database.session_factory,
            tmdb_client=AsyncMock(),
            pansou_client=AsyncMock(),
            crypto=AsyncMock(),
        )
        service._run_resource_search = AsyncMock()
        response = await service.start_resource_search(
            123, media_type=MediaType.MOVIE, season_number=None, refresh=False
        )
        assert response.status == "queued"
        assert response.snapshot_revision is None
        await asyncio.sleep(0)
        service._run_resource_search.assert_awaited_once()
        async with database.session_factory() as session:
            row = await session.get(ResourceSearchJob, "resource_search_stale")
            assert row is not None
            assert row.status == "queued"
            assert row.snapshot_revision is None
    finally:
        await database.engine.dispose()


def test_search_lock_cache_is_bounded():
    """长跑实例浏览大量影视时,进程内搜索锁 dict 只增不减会导致内存无界
    增长;容量上限必须生效,淘汰最旧条目。"""
    from watch_assistant.services.search import (
        _SEARCH_LOCK_CACHE_MAX,
        _bounded_setdefault,
    )

    cache: dict[tuple[str, int], object] = {}
    for index in range(_SEARCH_LOCK_CACHE_MAX + 10):
        _bounded_setdefault(
            cache, ("media", index), asyncio.Lock, _SEARCH_LOCK_CACHE_MAX
        )
    assert len(cache) == _SEARCH_LOCK_CACHE_MAX


def test_resource_search_task_cache_protects_in_flight_tasks():
    """资源搜索任务缓存的容量淘汰不得丢正在运行/排队中的在途任务。"""
    from watch_assistant.services.search import (
        _RESOURCE_SEARCH_TASK_CACHE_MAX,
        _bounded_set,
    )

    class _FakeTask:
        def __init__(self, task_id: str, status: str):
            self.task_id = task_id
            self.status = status

    cache: dict[str, _FakeTask] = {}
    for index in range(_RESOURCE_SEARCH_TASK_CACHE_MAX):
        _bounded_set(
            cache,
            f"key-{index}",
            _FakeTask(f"task-{index}", "ready"),
            _RESOURCE_SEARCH_TASK_CACHE_MAX,
            protect=lambda task: task.status in {"queued", "running"},
        )
    # 全部是 ready,新插入一个 running 必须保留,淘汰一个 ready
    _bounded_set(
        cache,
        "key-inflight",
        _FakeTask("task-inflight", "running"),
        _RESOURCE_SEARCH_TASK_CACHE_MAX,
        protect=lambda task: task.status in {"queued", "running"},
    )
    assert len(cache) == _RESOURCE_SEARCH_TASK_CACHE_MAX
    assert "key-inflight" in cache  # running 任务不被淘汰

    # 再插入一个 ready,仍然保留 running
    _bounded_set(
        cache,
        "key-extra",
        _FakeTask("task-extra", "ready"),
        _RESOURCE_SEARCH_TASK_CACHE_MAX,
        protect=lambda task: task.status in {"queued", "running"},
    )
    assert "key-inflight" in cache
    assert "key-extra" in cache
