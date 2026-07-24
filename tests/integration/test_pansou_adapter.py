import httpx
import pytest
import respx

from watch_assistant.adapters.pansou import PanSouClient, PanSouError
from watch_assistant.adapters.tmdb import TmdbClient


@respx.mock
async def test_pansou_client_returns_merged_result_shape():
    route = respx.get("http://pansou.test/api/search", params={"kw": "Inception 2010"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {"total": 1, "merged_by_type": {"magnet": []}},
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Inception 2010")
    await client.aclose()

    assert result == {"total": 1, "merged_by_type": {"magnet": []}}
    assert route.called


@respx.mock
async def test_pansou_timeout_is_reported_without_request_details():
    respx.get("http://pansou.test/api/search").mock(
        side_effect=httpx.ReadTimeout("secret query and internal URL")
    )
    client = PanSouClient("http://pansou.test", timeout=0.01)

    with pytest.raises(PanSouError, match="PanSou request failed") as error:
        await client.search("secret movie")
    await client.aclose()

    assert "secret" not in str(error.value)


@respx.mock
async def test_pansou_rejects_unexpected_response_shape():
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": []})
    )
    client = PanSouClient("http://pansou.test")

    with pytest.raises(PanSouError, match="response shape"):
        await client.search("Movie")
    await client.aclose()


@respx.mock
async def test_tmdb_client_extracts_movie_metadata():
    respx.get("https://api.themoviedb.org/3/movie/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 123,
                "title": "盗梦空间",
                "original_title": "Inception",
                "release_date": "2010-07-16",
                "overview": "Dreams within dreams.",
                "poster_path": "/poster.jpg",
            },
        )
    )
    client = TmdbClient("tmdb-secret")

    movie = await client.get_movie(123)
    await client.aclose()

    assert movie.tmdb_id == 123
    assert movie.release_year == 2010
    assert movie.original_title == "Inception"
