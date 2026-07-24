from unittest.mock import AsyncMock

from watch_assistant.models import SourceReliability
from watch_assistant.schemas import MediaType, MovieMetadata, SearchResponse
from watch_assistant.services.search import (
    SearchService,
    make_cache_key,
    source_penalty,
)


def test_cache_key_is_versioned_by_movie_id():
    assert make_cache_key(12345) == "tmdb:movie:12345:queries:v3"
    assert make_cache_key(12345, MediaType.TV) == "tmdb:tv:12345:queries:v3"


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
