import httpx

from watch_assistant.adapters.tmdb import TmdbClient, _parse_season
from watch_assistant.services.media_matcher import MediaMatchInput


def test_parse_tmdb_season_keeps_independent_identity_and_episode_summary():
    result = _parse_season(
        {
            "id": 456,
            "name": "Season 2",
            "overview": "Season overview",
            "air_date": "2025-01-01",
            "poster_path": "/poster.jpg",
            "episode_count": 1,
            "vote_average": 8.4,
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Episode 1",
                    "overview": "Episode overview",
                    "air_date": "2025-01-02",
                    "runtime": 45,
                    "vote_average": 8.1,
                }
            ],
        },
        series_tmdb_id=1399,
        season_number=2,
        language="zh-CN",
    )

    assert result.tmdb_season_id == 456
    assert result.series_tmdb_id == 1399
    assert result.overview_language == "zh-CN"
    assert result.episodes[0].episode_number == 1
    assert result.episodes[0].runtime == 45


async def test_search_candidates_enriches_bounded_detail_without_sensitive_fields():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/search/multi":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1399,
                            "media_type": "tv",
                            "name": "示例剧",
                            "original_name": "Example Show",
                            "first_air_date": "2024-01-01",
                            "origin_country": ["US"],
                        },
                        {"id": 77, "media_type": "person", "name": "Ignored"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 1399,
                "name": "示例剧",
                "original_name": "Example Show",
                "first_air_date": "2024-01-01",
                "origin_country": ["US"],
                "seasons": [{"season_number": 1, "episode_count": 8}],
                "cookie": "must-not-be-returned",
            },
        )

    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://tmdb.test"
    )
    client = TmdbClient("test-key", client=async_client)
    candidates = await client.search_candidates(
        MediaMatchInput(title="示例剧", media_type_hint="tv")
    )

    assert len(candidates) == 1
    assert candidates[0].tmdb_id == 1399
    assert candidates[0].seasons[0].episode_count == 8
    assert candidates[0].origin_countries == ("US",)
    assert "cookie" not in repr(candidates[0])
    assert [request.url.path for request in requests] == [
        "/search/multi",
        "/tv/1399",
    ]
    await async_client.aclose()
