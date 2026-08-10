import httpx
import pytest
import respx

from watch_assistant.adapters.tmdb import (
    TmdbClient,
    TmdbRateLimitedError,
)


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
