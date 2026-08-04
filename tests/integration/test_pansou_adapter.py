import base64

import httpx
import pytest
import respx

from watch_assistant.adapters.pansou import (
    _MAX_RESPONSE_BYTES,
    LinkCheckItem,
    LinkCheckState,
    PanSouClient,
    PanSouError,
    _magnet_infohash,
    _parse_plugin_result,
)
from watch_assistant.adapters.tmdb import (
    TmdbClient,
    _parse_media,
    build_search_queries,
)
from watch_assistant.schemas import MediaType, MovieMetadata, ResourceKind
from watch_assistant.services.normalize import normalize_pansou


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
async def test_pansou_falls_back_to_result_links_and_filters_unsupported_types():
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 1,
                    "results": [
                        {
                            "title": "Baidu result",
                            "links": [
                                {
                                    "type": "baidu",
                                    "url": "https://pan.baidu.com/s/synthetic",
                                    "password": "fixture-password",
                                    "datetime": "2026-08-04T00:00:00Z",
                                }
                            ],
                        }
                    ],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Baidu only")
    await client.aclose()

    assert result["merged_by_type"]["baidu"][0]["note"] == "Baidu result"
    assert normalize_pansou(result, share_domains=("115.com",)) == []


@respx.mock
async def test_pansou_falls_back_to_magnet_and_115_result_links():
    magnet_hash = "a" * 40
    magnet_url = f"magnet:?xt=urn:btih:{magnet_hash}"
    share_url = "https://115.com/s/synthetic-share"
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "results": [
                        {
                            "title": "Fallback magnet",
                            "links": [
                                {
                                    "type": "magnet",
                                    "url": magnet_url,
                                    "datetime": "2026-08-04T00:00:00Z",
                                }
                            ],
                        },
                        {
                            "title": "Fallback share",
                            "links": [
                                {
                                    "type": "115",
                                    "url": share_url,
                                    "password": "fixture-password",
                                    "datetime": "2026-08-04T00:00:00Z",
                                }
                            ],
                        },
                    ],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Magnet and share")
    await client.aclose()

    resources = normalize_pansou(result, share_domains=("115.com",))
    assert {item.kind for item in resources} == {
        ResourceKind.MAGNET,
        ResourceKind.SHARE,
    }
    share = next(item for item in resources if item.kind == ResourceKind.SHARE)
    assert share.password == "fixture-password"


@respx.mock
async def test_pansou_supplements_partial_grouped_results_from_result_links():
    magnet_hash = "b" * 40
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 1,
                    "merged_by_type": {"115": []},
                    "results": [
                        {
                            "title": "Supplemental magnet",
                            "links": [
                                {
                                    "type": "magnet",
                                    "url": f"magnet:?xt=urn:btih:{magnet_hash}",
                                },
                                {
                                    "type": "magnet",
                                    "url": (
                                        "magnet:?xt=urn:btih:"
                                        f"{magnet_hash}&dn=Duplicate"
                                    ),
                                },
                            ],
                        }
                    ],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Partial grouped response")
    await client.aclose()

    assert result["merged_by_type"]["magnet"] == [
        {
            "url": f"magnet:?xt=urn:btih:{magnet_hash}",
            "note": "Supplemental magnet",
        }
    ]


@respx.mock
async def test_pansou_extracts_nyaa_and_tpb_plugin_metadata_by_infohash():
    nyaa_hash = "a" * 40
    tpb_hash = "b" * 40
    nyaa_base32 = base64.b32encode(bytes.fromhex(nyaa_hash)).decode().rstrip("=")
    tpb_base32 = base64.b32encode(bytes.fromhex(tpb_hash)).decode().rstrip("=")
    nyaa_result = {
        "message_id": "nyaa-message",
        "unique_id": "nyaa-unique",
        "channel": "",
        "datetime": "2026-07-25T00:00:00Z",
        "title": "Nyaa Movie",
        "content": "大小: 1.5 GiB",
        "links": [
            {
                "type": "magnet",
                "url": f"magnet:?xt=urn:btih:{nyaa_base32}",
                "password": "",
                "datetime": "2026-07-25T00:00:00Z",
                "work_title": "Nyaa Movie",
            }
        ],
        "tags": ["做种: 12"],
        "images": [],
    }
    tpb_result = {
        "message_id": "tpb-message",
        "unique_id": "tpb-unique",
        "channel": "",
        "datetime": "2026-07-25T00:00:00Z",
        "title": "TPB Movie",
        "content": "Size 1.67\u00a0GiB, ... Seeders: 34",
        "links": [
            {
                "type": "magnet",
                "url": f"magnet:?xt=urn:btih:{tpb_hash.upper()}",
                "password": "",
                "datetime": "2026-07-25T00:00:00Z",
                "work_title": "TPB Movie",
            }
        ],
        "tags": [],
        "images": [],
    }
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
                                "url": f"magnet:?xt=urn:btih:{nyaa_hash.upper()}",
                                "note": "Nyaa Movie",
                                "source": "plugin:nyaa",
                            },
                            {
                                "url": f"magnet:?xt=urn:btih:{tpb_base32}",
                                "note": "TPB Movie",
                                "source": "plugin:thepiratebay",
                            },
                        ]
                    },
                    "results": [nyaa_result, tpb_result],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    result = await client.search("Movie")
    await client.aclose()

    magnets = result["merged_by_type"]["magnet"]
    for search_result in result["results"]:
        assert "source" not in search_result
        assert "plugin" not in search_result
        for link in search_result["links"]:
            assert "content" not in link
            assert "tags" not in link
    assert magnets[0]["size"] == int(1.5 * 1024**3)
    assert magnets[0]["seeders"] == 12
    assert magnets[0]["size_source"] == "pansou"
    assert magnets[0]["seeders_source"] == "pansou"
    assert magnets[0]["seeders_observed_at"]
    assert magnets[1]["size"] == int(1.67 * 1024**3)
    assert magnets[1]["seeders"] == 34
    assert route.calls[0].request.url.params["res"] == "all"
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("content", "expected_size"),
    [
        ("文件大小: 2 GiB", 2 * 1024**3),
        ("Size 2 GiB", 2 * 1024**3),
        ("Size\u00a02 GiB", 2 * 1024**3),
        ("Size: 2 GiB", 2 * 1024**3),
        ("Some Title Size 1080p", None),
        ("Size 2", None),
        ("Size: -2 GiB", None),
        ("Size 2 XB", None),
        ("2 GiB", None),
    ],
)
def test_pansou_tpb_size_labels_are_strict(content, expected_size):
    result = {
        "channel": "",
        "content": f"{content}, Seeders: 34",
        "tags": ["Seeders: 34"],
    }

    parsed = _parse_plugin_result("plugin:thepiratebay", result)

    if expected_size is None:
        assert "size" not in parsed
    else:
        assert parsed["size"] == expected_size
    assert parsed["seeders"] == 34


def test_pansou_infohash_skips_zero_value_before_valid_identity():
    valid_hash = "a" * 40
    url = (
        "magnet:?xt=urn:btih:"
        + "0" * 40
        + "&xt=urn:btih:"
        + valid_hash
    )

    assert _magnet_infohash(url) == valid_hash


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
                                "source": "plugin:nyaa",
                            },
                            {
                                "url": f"magnet:?xt=urn:btih:{unknown_hash}",
                                "note": "Unknown",
                                "source": "plugin:other",
                            },
                        ]
                    },
                    "results": [
                        {
                            "message_id": "invalid-message",
                            "unique_id": "invalid-unique",
                            "channel": "",
                            "datetime": "2026-07-25T00:00:00Z",
                            "title": "Invalid",
                            "content": "大小: -1 GB",
                            "links": [
                                {
                                    "type": "magnet",
                                    "url": f"magnet:?xt=urn:btih:{invalid_hash}",
                                    "password": "",
                                    "datetime": "2026-07-25T00:00:00Z",
                                    "work_title": "Invalid",
                                }
                            ],
                            "tags": ["做种: -3"],
                            "images": [],
                        },
                        {
                            "message_id": "unknown-message",
                            "unique_id": "unknown-unique",
                            "channel": "",
                            "datetime": "2026-07-25T00:00:00Z",
                            "title": "Unknown",
                            "content": "大小: 99 TB, Seeders: 999",
                            "links": [
                                {
                                    "type": "magnet",
                                    "url": f"magnet:?xt=urn:btih:{unknown_hash}",
                                    "password": "",
                                    "datetime": "2026-07-25T00:00:00Z",
                                    "work_title": "Unknown",
                                }
                            ],
                            "tags": [],
                            "images": [],
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
async def test_pansou_keeps_existing_structured_magnet_fields():
    infohash = "e" * 40
    result = {
        "message_id": "structured-message",
        "unique_id": "structured-unique",
        "channel": "",
        "datetime": "2026-07-25T00:00:00Z",
        "title": "Structured",
        "content": "大小: 1 GB",
        "links": [
            {
                "type": "magnet",
                "url": f"magnet:?xt=urn:btih:{infohash}",
                "password": "",
                "datetime": "2026-07-25T00:00:00Z",
                "work_title": "Structured",
            }
        ],
        "tags": ["做种: 2"],
        "images": [],
    }
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "merged_by_type": {
                        "magnet": [
                            {
                                "url": f"magnet:?xt=urn:btih:{infohash}",
                                "note": "Structured",
                                "source": "plugin:nyaa",
                                "size": "4 GB",
                                "seeders": 8,
                            }
                        ]
                    },
                    "results": [result],
                },
            },
        )
    )
    client = PanSouClient("http://pansou.test")

    response = await client.search("Structured")
    await client.aclose()

    magnet = response["merged_by_type"]["magnet"][0]
    assert magnet["size"] == "4 GB"
    assert magnet["seeders"] == 8
    assert "size_source" not in magnet
    assert "seeders_source" not in magnet
    assert "seeders_observed_at" not in magnet


@pytest.mark.parametrize(
    "data",
    [
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": "magnet:?xt=urn:btih:ffffffffffffffffffffffffffffffffffffffff",
                        "source": "plugin:nyaa",
                    }
                ]
            }
        },
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": "magnet:?xt=urn:btih:ffffffffffffffffffffffffffffffffffffffff",
                        "source": "plugin:nyaa",
                    }
                ]
            },
            "results": [None, "invalid"],
        },
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": "magnet:?xt=urn:btih:ffffffffffffffffffffffffffffffffffffffff",
                        "source": "plugin:nyaa",
                    }
                ]
            },
            "results": [
                {
                    "links": [
                        {
                            "type": "magnet",
                            "url": "magnet:?xt=urn:btih:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        }
                    ],
                    "content": "大小: 1 GB",
                    "tags": ["做种: 3"],
                }
            ],
        },
    ],
)
@respx.mock
async def test_pansou_missing_or_malformed_results_degrade_safely(data):
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": data},
        )
    )
    client = PanSouClient("http://pansou.test")

    response = await client.search("No metadata")
    await client.aclose()

    assert response["merged_by_type"]["magnet"] == data["merged_by_type"]["magnet"]


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
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


async def test_pansou_does_not_follow_search_redirects():
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host or "")
        if len(requested_hosts) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://redirect-target.test/api/search"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"merged_by_type": {"magnet": []}}},
            request=request,
        )

    transport_client = httpx.AsyncClient(
        base_url="https://pansou.test",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    client = PanSouClient("https://pansou.test", client=transport_client)

    try:
        with pytest.raises(PanSouError):
            await client.search("Movie")
    finally:
        await client.aclose()
        await transport_client.aclose()

    assert requested_hosts == ["pansou.test"]


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
async def test_pansou_replaces_invalid_utf8_inside_json_text():
    route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'{"code":0,"data":{"total":1,"merged_by_type":'
                b'{"magnet":[{"url":"magnet:?xt=urn:btih:'
                b'abcdef0123456789abcdef0123456789abcdef01",'
                b'"note":"Movie \xff 1080p"}]}}}'
            ),
            headers={"content-type": "application/json"},
        )
    )
    client = PanSouClient("http://pansou.test")

    try:
        result = await client.search("Movie")
    finally:
        await client.aclose()

    assert route.called
    assert result["total"] == 1
    assert result["merged_by_type"]["magnet"][0]["note"] == "Movie � 1080p"


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
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            content=b'{"code":0,"data":{"merged_by_type":{}}}',
            headers={"content-type": "text/plain"},
        ),
        httpx.Response(
            200,
            content=b"x" * (_MAX_RESPONSE_BYTES + 1),
            headers={"content-type": "application/json"},
        ),
        httpx.Response(
            200,
            content=b"[" * 13 + b"0" + b"]" * 13,
            headers={"content-type": "application/json"},
        ),
        httpx.Response(
            200,
            content=b'{"message":"' + b"x" * 8193 + b'"}',
            headers={"content-type": "application/json"},
        ),
    ],
)
@respx.mock
async def test_pansou_rejects_unbounded_or_non_json_success_response(
    response: httpx.Response,
):
    respx.get("http://pansou.test/api/search").mock(return_value=response)
    client = PanSouClient("http://pansou.test")

    with pytest.raises(PanSouError, match="response shape") as error:
        await client.search("Movie")
    await client.aclose()

    assert error.value.__cause__ is None


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
