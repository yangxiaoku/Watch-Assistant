import httpx
import pytest
import respx

from watch_assistant.adapters.pansou import (
    LinkCheckItem,
    LinkCheckState,
    PanSouClient,
    PanSouError,
)
from watch_assistant.adapters.tmdb import (
    TmdbClient,
    _parse_media,
    build_search_queries,
)
from watch_assistant.schemas import MediaType, MovieMetadata


@respx.mock
async def test_pansou_client_returns_merged_result_shape():
    route = respx.get(
        "http://pansou.test/api/search",
        params={"kw": "Inception 2010", "res": "all"},
    ).mock(
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
async def test_pansou_extracts_nyaa_and_tpb_plugin_metadata_by_infohash():
    nyaa_hash = "a" * 40
    tpb_hash = "b" * 40
    route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "merged_by_type": {
                        "magnet": [
                            {
                                "url": f"magnet:?xt=urn:btih:{nyaa_hash}",
                                "note": "Nyaa Movie",
                            },
                            {
                                "url": f"magnet:?xt=urn:btih:{tpb_hash}",
                                "note": "TPB Movie",
                            },
                        ]
                    },
                    "results": [
                        {
                            "source": "plugin:nyaa",
                            "links": [
                                {
                                    "url": f"magnet:?xt=urn:btih:{nyaa_hash}",
                                    "Content": "大小: 1.5 GiB",
                                    "Tags": "做种: 12",
                                }
                            ],
                        },
                        {
                            "source": "plugin:thepiratebay",
                            "links": [
                                {
                                    "url": f"magnet:?xt=urn:btih:{tpb_hash}",
                                    "Content": "文件大小: 2 TB, Seeders: 34",
                                }
                            ],
                        },
                    ],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Movie")
    await client.aclose()

    magnets = result["merged_by_type"]["magnet"]
    assert magnets[0]["size"] == int(1.5 * 1024**3)
    assert magnets[0]["seeders"] == 12
    assert magnets[0]["size_source"] == "pansou"
    assert magnets[0]["seeders_source"] == "pansou"
    assert magnets[0]["seeders_observed_at"]
    assert magnets[1]["size"] == 2 * 1024**4
    assert magnets[1]["seeders"] == 34
    assert route.calls[0].request.url.params["res"] == "all"
    assert route.call_count == 1


@respx.mock
async def test_pansou_ignores_invalid_values_and_unknown_plugin_text():
    invalid_hash = "c" * 40
    unknown_hash = "d" * 40
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "merged_by_type": {
                        "magnet": [
                            {
                                "url": f"magnet:?xt=urn:btih:{invalid_hash}",
                                "note": "Invalid",
                            },
                            {
                                "url": f"magnet:?xt=urn:btih:{unknown_hash}",
                                "note": "Unknown",
                            },
                        ]
                    },
                    "results": [
                        {
                            "source": "plugin:nyaa",
                            "links": [
                                {
                                    "url": f"magnet:?xt=urn:btih:{invalid_hash}",
                                    "Content": "大小: -1 GB",
                                    "Tags": "做种: -3",
                                }
                            ],
                        },
                        {
                            "source": "plugin:other",
                            "links": [
                                {
                                    "url": f"magnet:?xt=urn:btih:{unknown_hash}",
                                    "Content": "大小: 99 TB, Seeders: 999",
                                }
                            ],
                        },
                    ],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Movie")
    await client.aclose()

    assert "size" not in result["merged_by_type"]["magnet"][0]
    assert "seeders" not in result["merged_by_type"]["magnet"][0]
    assert "size" not in result["merged_by_type"]["magnet"][1]
    assert "seeders" not in result["merged_by_type"]["magnet"][1]


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
                        "seasons": [{"season_number": 1}],
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
    assert movies[0].seasons == []


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
                "seasons": [
                    {
                        "season_number": 2,
                        "name": "Season 2",
                        "episode_count": 10,
                        "air_date": "2012-01-01",
                        "poster_path": "/season-2.jpg",
                    }
                ],
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
    assert show.seasons[0].season_number == 2
    assert show.seasons[0].episode_count == 10
    assert results.page == 2
    assert results.total_pages == 8
    assert [item.tmdb_id for item in results.results] == [1399]


def test_tmdb_season_parser_filters_invalid_values_and_stably_deduplicates():
    show = _parse_media(
        {
            "name": "示例剧",
            "original_name": "Example Show",
            "seasons": [
                {"season_number": 2, "name": "  "},
                {"season_number": True, "name": "bad bool"},
                {"season_number": -1, "name": "bad negative"},
                {
                    "season_number": 0,
                    "name": " Specials ",
                    "episode_count": 2,
                },
                {
                    "season_number": 2,
                    "name": "duplicate should be ignored",
                    "episode_count": 99,
                },
                {"season_number": 3, "name": "Season 3", "episode_count": True},
            ],
        },
        tmdb_id=1,
        media_type=MediaType.TV,
    )

    assert [season.season_number for season in show.seasons] == [0, 2, 3]
    assert show.seasons[0].name == "Specials"
    assert show.seasons[1].name == "Season 2"
    assert show.seasons[1].episode_count == 0
    assert show.seasons[2].episode_count == 0


def test_build_search_queries_appends_four_selected_tv_season_queries():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="权力的游戏",
        original_title="Game of Thrones",
        release_year=2011,
    )

    queries = build_search_queries(show, season_number=2)

    assert queries[:4] == [
        "权力的游戏",
        "权力的游戏 2011",
        "Game of Thrones",
        "Game of Thrones 2011",
    ]
    assert queries[4:] == [
        "权力的游戏 第2季",
        "权力的游戏 S02",
        "Game of Thrones Season 2",
        "Game of Thrones S02",
    ]


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
