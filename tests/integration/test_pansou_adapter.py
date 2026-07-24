import httpx
import pytest
import respx

from watch_assistant.adapters.pansou import (
    LinkCheckItem,
    LinkCheckState,
    PanSouClient,
    PanSouError,
)
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.schemas import MediaType


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
async def test_pansou_accepts_empty_result_without_merged_groups():
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "message": "success", "data": {"total": 0}},
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("No results")
    await client.aclose()

    assert result == {"total": 0, "merged_by_type": {}}


@respx.mock
async def test_pansou_checks_115_links_in_batches():
    route = respx.post("http://pansou.test/api/check/links").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"state": "ok"}, {"state": "bad"}]},
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {"state": "locked"},
                        {"state": "unsupported"},
                    ]
                },
            ),
        ]
    )
    client = PanSouClient("http://pansou.test")

    states = await client.check_links(
        [
            LinkCheckItem("https://115.com/s/one", "a"),
            LinkCheckItem("https://115.com/s/two", "b"),
            LinkCheckItem("https://115.com/s/three", None),
            LinkCheckItem("https://115.com/s/four", None),
        ],
        batch_size=2,
    )
    await client.aclose()

    assert states == [
        LinkCheckState.OK,
        LinkCheckState.BAD,
        LinkCheckState.LOCKED,
        LinkCheckState.UNSUPPORTED,
    ]
    assert route.call_count == 2
    assert b'"disk_type":"115"' in route.calls[0].request.content
    assert b'"password":"a"' in route.calls[0].request.content


@respx.mock
async def test_pansou_link_check_failure_redacts_link_details():
    respx.post("http://pansou.test/api/check/links").mock(
        side_effect=httpx.ReadTimeout("https://115.com/s/secret")
    )
    client = PanSouClient("http://pansou.test")

    with pytest.raises(PanSouError, match="link check failed") as error:
        await client.check_links(
            [LinkCheckItem("https://115.com/s/secret", "password")]
        )
    await client.aclose()

    assert "secret" not in str(error.value)


@respx.mock
async def test_pansou_rejects_malformed_link_check_rows():
    respx.post("http://pansou.test/api/check/links").mock(
        return_value=httpx.Response(200, json={"results": ["not-an-object"]})
    )
    client = PanSouClient("http://pansou.test")

    with pytest.raises(PanSouError, match="response shape"):
        await client.check_links([LinkCheckItem("https://115.com/s/one", None)])
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
                "backdrop_path": "/backdrop.jpg",
                "genres": [{"id": 878, "name": "科幻"}],
            },
        )
    )
    client = TmdbClient("tmdb-secret")

    movie = await client.get_movie(123)
    await client.aclose()

    assert movie.tmdb_id == 123
    assert movie.release_year == 2010
    assert movie.original_title == "Inception"
    assert movie.backdrop_path == "/backdrop.jpg"
    assert movie.genre_ids == [878]


@respx.mock
async def test_tmdb_client_returns_popular_movies():
    route = respx.get("https://api.themoviedb.org/3/movie/popular").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 550,
                        "title": "搏击俱乐部",
                        "original_title": "Fight Club",
                        "release_date": "1999-10-15",
                        "poster_path": "/fight-club.jpg",
                        "vote_average": 8.4,
                    }
                ]
            },
        )
    )
    client = TmdbClient("tmdb-secret")

    movies = await client.get_popular()
    await client.aclose()

    assert route.called
    assert movies[0].tmdb_id == 550
    assert movies[0].vote_average == 8.4


@respx.mock
async def test_tmdb_client_parses_tv_and_multi_search_pagination():
    respx.get("https://api.themoviedb.org/3/tv/1399").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1399,
                "name": "权力的游戏",
                "original_name": "Game of Thrones",
                "first_air_date": "2011-04-17",
                "genres": [{"id": 10765}],
            },
        )
    )
    respx.get("https://api.themoviedb.org/3/search/multi").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 2,
                "total_pages": 8,
                "total_results": 150,
                "results": [
                    {
                        "id": 1399,
                        "media_type": "tv",
                        "name": "权力的游戏",
                        "first_air_date": "2011-04-17",
                    },
                    {"id": 1, "media_type": "person", "name": "演员"},
                ],
            },
        )
    )
    client = TmdbClient("tmdb-secret")

    show = await client.get_media(1399, MediaType.TV)
    results = await client.search_media("权力的游戏", page=2)
    await client.aclose()

    assert show.title == "权力的游戏"
    assert show.original_title == "Game of Thrones"
    assert show.release_year == 2011
    assert show.media_type == MediaType.TV
    assert results.page == 2
    assert results.total_pages == 8
    assert [item.tmdb_id for item in results.results] == [1399]


@respx.mock
async def test_tmdb_client_prioritizes_movie_and_tv_alternative_titles():
    respx.get("https://api.themoviedb.org/3/movie/278/alternative_titles").mock(
        return_value=httpx.Response(
            200,
            json={
                "titles": [
                    {"iso_3166_1": "US", "title": "Shawshank"},
                    {"iso_3166_1": "HK", "title": "Moonlight Flight"},
                    {"iso_3166_1": "CN", "title": "Redemption"},
                ]
            },
        )
    )
    respx.get("https://api.themoviedb.org/3/tv/1399/alternative_titles").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"iso_3166_1": "US", "title": "Thrones"},
                    {"iso_3166_1": "TW", "title": "Power Game"},
                ]
            },
        )
    )
    client = TmdbClient("tmdb-secret")

    movie_titles = await client.get_alternative_titles(278, MediaType.MOVIE)
    tv_titles = await client.get_alternative_titles(1399, MediaType.TV)
    await client.aclose()

    assert movie_titles == ["Redemption", "Moonlight Flight", "Shawshank"]
    assert tv_titles == ["Power Game", "Thrones"]
