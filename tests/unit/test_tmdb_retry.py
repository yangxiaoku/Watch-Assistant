import httpx
import pytest
import respx

from watch_assistant.adapters.tmdb import (
    TmdbClient,
    TmdbRateLimitedError,
)
from watch_assistant.schemas import MediaType
from watch_assistant.services.media_matcher import MediaKind, MediaMatchInput


def _search_result(media_id: int, media_type: str, title: str) -> dict:
    return {
        "id": media_id,
        "media_type": media_type,
        "title": title,
        "original_title": f"Original {media_id}",
        "release_date": "2020-01-01",
        "vote_average": 7.5,
        "adult": False,
        "genre_ids": [28],
    }


def _detail_payload(media_id: int, media_type: str, title: str) -> dict:
    payload = _search_result(media_id, media_type, title)
    payload["origin_country"] = ["CN"]
    if media_type == "tv":
        payload["seasons"] = [
            {"season_number": 1, "episode_count": 10, "name": "Season 1"}
        ]
    return payload


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_429_is_retried_with_backoff_and_succeeds():
    route = respx.get("https://api.themoviedb.org/3/test").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = TmdbClient("test-key")
    payload = await client._get("/test")
    assert payload == {"ok": True}
    assert route.call_count == 3
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_429_after_retries_raises_rate_limited_with_retry_after():
    route = respx.get("https://api.themoviedb.org/3/test").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1.5"}),
            httpx.Response(429, headers={"Retry-After": "1.5"}),
            httpx.Response(429, headers={"Retry-After": "1.5"}),
        ]
    )
    client = TmdbClient("test-key")
    with pytest.raises(TmdbRateLimitedError) as error:
        await client._get("/test")
    assert error.value.retry_after_seconds == 1.5
    assert route.call_count == 3
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_5xx_is_retried_and_auth_errors_are_not():
    ok = respx.get("https://api.themoviedb.org/3/ok").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    client = TmdbClient("test-key")
    assert await client._get("/ok") == {"ok": True}
    assert ok.call_count == 2

    bad = respx.get("https://api.themoviedb.org/3/bad").mock(
        return_value=httpx.Response(401)
    )
    from watch_assistant.adapters.tmdb import TmdbAuthError

    with pytest.raises(TmdbAuthError):
        await client._get("/bad")
    assert bad.call_count == 1  # 认证错误不重试
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_candidate_enrichment_is_bounded_ordered_and_skips_unmatched():
    """并发富化保持结果顺序,达到 limit 后不再发请求,类型不匹配不发请求。"""
    respx.get("https://api.themoviedb.org/3/search/multi").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _search_result(101, "movie", "候选 A"),
                    _search_result(102, "movie", "候选 B"),
                    _search_result(201, "tv", "剧集 C"),
                    _search_result(103, "movie", "候选 D"),
                    _search_result(104, "movie", "候选 E"),
                ]
            },
        )
    )
    detail_routes: dict[tuple[str, int], respx.Route] = {}
    for media_type, media_id, title in (
        ("movie", 101, "候选 A"),
        ("movie", 102, "候选 B"),
        ("tv", 201, "剧集 C"),
        ("movie", 103, "候选 D"),
        ("movie", 104, "候选 E"),
    ):
        detail_routes[(media_type, media_id)] = respx.get(
            f"https://api.themoviedb.org/3/{media_type}/{media_id}"
        ).mock(
            return_value=httpx.Response(
                200, json=_detail_payload(media_id, media_type, title)
            )
        )

    client = TmdbClient("test-key")
    query = MediaMatchInput(
        title="候选", year=2020, media_type_hint="movie", kind=MediaKind.MOVIE.value
    )
    candidates = await client.search_candidates(query, limit=3)
    await client.aclose()

    assert [item.tmdb_id for item in candidates] == [101, 102, 103]
    # 每个候选 zh + en 各一次详情请求。
    assert detail_routes[("movie", 101)].call_count == 2
    assert detail_routes[("movie", 102)].call_count == 2
    assert detail_routes[("movie", 103)].call_count == 2
    # 达到 limit 后不再富化后续项;类型不匹配的项从不请求详情。
    assert detail_routes[("movie", 104)].call_count == 0
    assert detail_routes[("tv", 201)].call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_text_search_cache_dedupes_hot_queries():
    """规范化后的同一热词只打一次 TMDB。"""
    route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=httpx.Response(
            200, json={"results": [_search_result(1, "movie", "Inception")]}
        )
    )
    client = TmdbClient("test-key")
    first = await client.search_movies("  Inception ")
    second = await client.search_movies("inception")
    third = await client.search_movies("INCEPTION")

    assert [item.tmdb_id for item in first] == [item.tmdb_id for item in second]
    assert [item.tmdb_id for item in first] == [item.tmdb_id for item in third]
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tmdb_get_media_is_cached_within_ttl():
    """重复 get_media 在 TTL 内只打一次 TMDB。"""
    route = respx.get("https://api.themoviedb.org/3/movie/123").mock(
        return_value=httpx.Response(
            200, json=_detail_payload(123, "movie", "盗梦空间")
        )
    )
    client = TmdbClient("test-key")
    first = await client.get_media(123, MediaType.MOVIE)
    second = await client.get_media(123, MediaType.MOVIE)

    assert first == second
    assert first.tmdb_id == 123
    assert route.call_count == 1
    await client.aclose()
