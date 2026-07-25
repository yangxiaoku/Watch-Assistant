from datetime import UTC, datetime
from unittest.mock import AsyncMock

from watch_assistant.models import Resource, SourceReliability
from watch_assistant.schemas import (
    MediaType,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    SearchResponse,
)
from watch_assistant.services.search import (
    SearchService,
    _limit_magnet_resources,
    _quality_matches,
    _resource_sort_key,
    make_cache_key,
    source_penalty,
)


def test_cache_key_is_versioned_by_movie_id():
    assert make_cache_key(12345) == "tmdb:movie:12345:queries:v4"
    assert make_cache_key(12345, MediaType.TV) == "tmdb:tv:12345:queries:v4"
    assert make_cache_key(12345, MediaType.TV, 2) == (
        "tmdb:tv:12345:season:2:queries:v4"
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
    assert sorted(
        (second, first),
        key=lambda item: _resource_sort_key(item, tied_scores, "comprehensive"),
    ) == [first, second]


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
