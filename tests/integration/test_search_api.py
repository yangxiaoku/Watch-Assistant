import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import (
    MovieWatch,
    Resource,
    SearchCache,
    SourceReliability,
)
from watch_assistant.schemas import MovieMetadata

TMDB_RESPONSE = {
    "id": 12345,
    "title": "盗梦空间",
    "original_title": "Inception",
    "release_date": "2010-07-16",
    "overview": "Dreams within dreams.",
    "poster_path": "/poster.jpg",
}
MAGNET = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
SHARE_URL = "https://115.com/s/shareCode1"


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("tmdb-secret")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        share_domains=("115.com",),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou


async def _close(client, database, tmdb, pansou):
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


def _mock_tmdb():
    return respx.get("https://api.themoviedb.org/3/movie/12345").mock(
        return_value=httpx.Response(200, json=TMDB_RESPONSE)
    )


def _pansou_response():
    return {
        "code": 0,
        "message": "success",
        "data": {
            "total": 2,
            "merged_by_type": {
                "magnet": [
                    {
                        "url": MAGNET,
                        "note": "Inception 2010 2160p",
                        "source": "plugin:magnet",
                        "datetime": "2026-07-23T10:30:00Z",
                    }
                ],
                "115": [
                    {
                        "url": SHARE_URL,
                        "password": "share-secret",
                        "note": "Inception 2010 BluRay",
                        "source": "plugin:share",
                        "datetime": "2026-07-23T11:30:00Z",
                    }
                ],
            },
        },
    }


def _empty_pansou_response():
    return {"code": 0, "data": {"total": 0, "merged_by_type": {}}}


@pytest.mark.integration
@respx.mock
async def test_search_persists_encrypted_resources_and_returns_only_ids(tmp_path):
    _mock_tmdb()
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["resource_id"].startswith("res_")
    assert "url" not in body["results"][0]
    assert "password" not in body["results"][0]
    assert MAGNET not in response.text
    assert "share-secret" not in response.text
    async with database.session_factory() as session:
        resources = list(await session.scalars(select(Resource)))
        cache = await session.scalar(select(SearchCache))
    assert len(resources) == 2
    assert all(MAGNET not in item.encrypted_url for item in resources)
    assert all(
        "share-secret" not in (item.encrypted_password or "") for item in resources
    )
    assert MAGNET not in cache.resource_ids_json
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_search_returns_only_top_30_magnets_and_keeps_shares(tmp_path):
    _mock_tmdb()
    magnets = [
        {
            "url": f"magnet:?xt=urn:btih:{index:040x}",
            "note": f"Inception 2010 1080p release-{index}",
            "source": "plugin:bulk",
            "seeders": index,
            "size": "4 GB",
        }
        for index in range(1, 101)
    ]
    payload = _pansou_response()
    payload["data"]["merged_by_type"] = {
        "magnet": magnets,
        "115": [payload["data"]["merged_by_type"]["115"][0]],
    }
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    first = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    second = await client.post("/api/v1/search", json={"tmdb_id": 12345})

    assert first.status_code == 200
    assert len(first.json()["results"]) == 31
    assert sum(item["kind"] == "magnet" for item in first.json()["results"]) == 30
    assert sum(item["kind"] == "115_share" for item in first.json()["results"]) == 1
    assert [
        item["name"] for item in first.json()["results"] if item["kind"] == "magnet"
    ] == [f"Inception 2010 1080p release-{index}" for index in range(100, 70, -1)]
    assert all(
        0 <= item[field] <= 100
        for item in first.json()["results"]
        for field in ("rank_score", "relevance_score", "completeness_score")
    )
    assert [item["resource_id"] for item in first.json()["results"]] == [
        item["resource_id"] for item in second.json()["results"]
    ]
    assert [
        (item["rank_score"], item["relevance_score"], item["completeness_score"])
        for item in first.json()["results"]
    ] == [
        (item["rank_score"], item["relevance_score"], item["completeness_score"])
        for item in second.json()["results"]
    ]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_search_uses_stale_cache_when_pansou_fails(tmp_path):
    _mock_tmdb()
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    first = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    assert first.status_code == 200
    pansou_route.side_effect = httpx.ReadTimeout("upstream unavailable")

    response = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "refresh": True}
    )

    assert response.status_code == 200
    assert response.json()["cached"] is True
    assert "stale_cache" in response.json()["warnings"]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_partial_positive_refresh_preserves_existing_cache(tmp_path):
    _mock_tmdb()
    route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    first = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    calls = 0

    def partial_response(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 1,
                        "merged_by_type": {
                            "magnet": [
                                {
                                    "url": (
                                        "magnet:?xt=urn:btih:"
                                        "1111111111111111111111111111111111111111"
                                    ),
                                    "note": "Inception 2010 1080p",
                                    "source": "plugin:partial",
                                }
                            ]
                        },
                    },
                },
            )
        raise httpx.ReadTimeout("one query failed")

    route.side_effect = partial_response

    refreshed = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "refresh": True}
    )

    assert first.status_code == 200
    assert refreshed.status_code == 200
    assert refreshed.json()["cached"] is True
    assert len(refreshed.json()["results"]) == 2
    assert "partial_upstream" in refreshed.json()["warnings"]
    assert "stale_cache" in refreshed.json()["warnings"]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_concurrent_searches_share_one_four_query_refresh(tmp_path):
    _mock_tmdb()
    route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    responses = await asyncio.gather(
        client.post("/api/v1/search", json={"tmdb_id": 12345}),
        client.post("/api/v1/search", json={"tmdb_id": 12345}),
    )

    assert all(response.status_code == 200 for response in responses)
    assert route.call_count == 4
    assert sorted(response.json()["cached"] for response in responses) == [
        False,
        True,
    ]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_force_refresh_removes_bad_115_share(tmp_path):
    _mock_tmdb()
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    check_route = respx.post("http://pansou.test/api/check/links").mock(
        return_value=httpx.Response(200, json={"results": [{"state": "bad"}]})
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    await client.post("/api/v1/search", json={"tmdb_id": 12345})

    response = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "refresh": True}
    )

    assert response.status_code == 200
    assert check_route.called
    assert {item["kind"] for item in response.json()["results"]} == {"magnet"}
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("media_type", "tmdb_id", "metadata", "alternatives", "alias", "note"),
    [
        (
            "movie",
            278,
            {
                "id": 278,
                "title": "Shawshank",
                "original_title": "The Shawshank Redemption",
                "release_date": "1994-09-23",
            },
            {"titles": [{"iso_3166_1": "HK", "title": "Moonlight Flight"}]},
            "Moonlight Flight",
            "Moonlight Flight 1994 1080p",
        ),
        (
            "tv",
            1399,
            {
                "id": 1399,
                "name": "Thrones",
                "original_name": "Game of Thrones",
                "first_air_date": "2011-04-17",
            },
            {"results": [{"iso_3166_1": "TW", "title": "Power Game"}]},
            "Power Game",
            "Power Game S08 2019 1080p",
        ),
    ],
)
@respx.mock
async def test_search_uses_media_alternatives_only_after_zero_candidates(
    tmp_path, media_type, tmdb_id, metadata, alternatives, alias, note
):
    respx.get(f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}").mock(
        return_value=httpx.Response(200, json=metadata)
    )
    alternative_route = respx.get(
        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/alternative_titles"
    ).mock(return_value=httpx.Response(200, json=alternatives))

    def pansou_response(request: httpx.Request) -> httpx.Response:
        if request.url.params["kw"] != alias:
            return httpx.Response(200, json=_empty_pansou_response())
        payload = {
            "code": 0,
            "data": {
                "total": 1,
                "merged_by_type": {
                    "magnet": [{"url": MAGNET, "note": note, "source": "plugin:alias"}]
                },
            },
        }
        return httpx.Response(200, json=payload)

    route = respx.get("http://pansou.test/api/search").mock(side_effect=pansou_response)
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tmdb_id, "media_type": media_type},
    )

    assert response.status_code == 200
    assert alternative_route.called
    assert route.call_count == 5
    assert len(response.json()["results"]) == 1
    assert "alternative_titles_used" in response.json()["warnings"]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_empty_watch_uses_composite_key_and_closes_when_found(tmp_path):
    respx.get("https://api.themoviedb.org/3/tv/12345").mock(
        return_value=httpx.Response(
            200,
            json={"id": 12345, "name": "Lost", "first_air_date": "2004-09-22"},
        )
    )
    respx.get("https://api.themoviedb.org/3/tv/12345/alternative_titles").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    empty = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "media_type": "tv"}
    )
    async with database.session_factory() as session:
        watch = await session.get(MovieWatch, ("tv", 12345))
        assert watch is not None and watch.active is True

    found_payload = {
        "code": 0,
        "data": {
            "total": 1,
            "merged_by_type": {
                "magnet": [
                    {"url": MAGNET, "note": "Lost S01E01", "source": "plugin:tv"}
                ]
            },
        },
    }
    route.return_value = httpx.Response(200, json=found_payload)
    found = await client.post(
        "/api/v1/search",
        json={"tmdb_id": 12345, "media_type": "tv", "refresh": True},
    )

    assert "watching_for_resources" in empty.json()["warnings"]
    assert "new_resources_found" in found.json()["warnings"]
    async with database.session_factory() as session:
        watch = await session.get(MovieWatch, ("tv", 12345))
        assert watch is not None and watch.active is False
        assert watch.found_at is not None
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_same_tmdb_id_uses_distinct_movie_and_tv_search_locks(tmp_path):
    _mock_tmdb()
    respx.get("https://api.themoviedb.org/3/tv/12345").mock(
        return_value=httpx.Response(
            200,
            json={"id": 12345, "name": "Lost", "first_air_date": "2004-09-22"},
        )
    )

    def pansou_response(request: httpx.Request) -> httpx.Response:
        note = (
            "Lost S01E01"
            if "Lost" in request.url.params["kw"]
            else "Inception 2010 1080p"
        )
        payload = _pansou_response()
        payload["data"]["merged_by_type"] = {
            "magnet": [{"url": MAGNET, "note": note, "source": "plugin:media"}]
        }
        return httpx.Response(200, json=payload)

    respx.get("http://pansou.test/api/search").mock(side_effect=pansou_response)
    client, database, tmdb, pansou = await _make_client(tmp_path)

    responses = await asyncio.gather(
        client.post("/api/v1/search", json={"tmdb_id": 12345, "media_type": "movie"}),
        client.post("/api/v1/search", json={"tmdb_id": 12345, "media_type": "tv"}),
    )

    service = client._transport.app.state.search_service
    assert all(response.status_code == 200 for response in responses)
    assert set(service._search_locks) == {("movie", 12345), ("tv", 12345)}
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_source_reliability_is_recorded_and_affects_ranking(tmp_path):
    _mock_tmdb()
    payload = {
        "code": 0,
        "data": {
            "total": 2,
            "merged_by_type": {
                "magnet": [
                    {
                        "url": MAGNET,
                        "note": "Inception 2010 2160p",
                        "source": "plugin:bad",
                    },
                    {
                        "url": (
                            "magnet:?xt=urn:btih:"
                            "1111111111111111111111111111111111111111"
                        ),
                        "note": "Inception 2010 1080p",
                        "source": "plugin:good",
                    },
                ]
            },
        },
    }
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    async with database.session_factory() as session:
        session.add(
            SourceReliability(
                source="plugin:bad",
                accepted_count=1,
                rejected_count=9,
                link_ok_count=0,
                link_bad_count=0,
            )
        )
        await session.commit()

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})

    assert [item["source"] for item in response.json()["results"]] == [
        "plugin:good",
        "plugin:bad",
    ]
    async with database.session_factory() as session:
        bad = await session.get(SourceReliability, "plugin:bad")
        good = await session.get(SourceReliability, "plugin:good")
    assert bad.accepted_count == 2
    assert good.accepted_count == 1
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_search_returns_502_when_pansou_fails_without_cache(tmp_path):
    _mock_tmdb()
    respx.get("http://pansou.test/api/search").mock(
        side_effect=httpx.ReadTimeout("upstream unavailable")
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})

    assert response.status_code == 502
    assert response.json()["detail"] == "pansou_unavailable"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_search_reuses_existing_canonical_resource_id(tmp_path):
    _mock_tmdb()
    respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_legacy",
                kind="magnet",
                canonical_key=("magnet:abcdef0123456789abcdef0123456789abcdef01"),
                encrypted_url="legacy-ciphertext",
                name="Legacy name",
                source="legacy",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC),
            )
        )
        await session.commit()

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})

    assert response.status_code == 200
    magnet = next(
        item for item in response.json()["results"] if item["kind"] == "magnet"
    )
    assert magnet["resource_id"] == "res_legacy"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_popular_movies_endpoint_returns_browse_catalog(tmp_path):
    respx.get("https://api.themoviedb.org/3/movie/popular").mock(
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
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.get("/api/v1/movies/popular")

    assert response.status_code == 200
    assert response.json()["results"][0]["tmdb_id"] == 550
    assert response.json()["results"][0]["vote_average"] == 8.4
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_movie_title_search_endpoint(tmp_path):
    route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 27205, "title": "盗梦空间"}]},
        )
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.get("/api/v1/movies/search", params={"query": "盗梦空间"})

    assert response.status_code == 200
    assert response.json()["results"][0]["tmdb_id"] == 27205
    assert route.called
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_home_catalog_returns_real_tmdb_sections(tmp_path):
    feeds = (
        ("movie", "popular"),
        ("movie", "now_playing"),
        ("movie", "upcoming"),
        ("movie", "top_rated"),
        ("tv", "popular"),
        ("tv", "on_the_air"),
        ("tv", "top_rated"),
    )
    for index, (media_type, feed) in enumerate(feeds):
        title_field = "title" if media_type == "movie" else "name"
        respx.get(f"https://api.themoviedb.org/3/{media_type}/{feed}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 100 + index,
                            title_field: f"{media_type}-{feed}",
                            "backdrop_path": f"/{feed}.jpg",
                            "genre_ids": [18],
                        }
                    ]
                },
            )
        )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.get("/api/v1/movies/home")

    assert response.status_code == 200
    assert set(response.json()) == {
        "popular",
        "now_playing",
        "upcoming",
        "top_rated",
        "tv_popular",
        "tv_on_the_air",
        "tv_top_rated",
    }
    assert response.json()["popular"][0]["backdrop_path"] == "/popular.jpg"
    assert response.json()["top_rated"][0]["genre_ids"] == [18]
    assert response.json()["tv_popular"][0]["media_type"] == "tv"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_discover_movies_forwards_filters_to_tmdb(tmp_path):
    route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 680, "title": "低俗小说"}]},
        )
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.get(
        "/api/v1/movies/discover",
        params={"genre_id": 80, "year": 1994, "sort": "rating"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["tmdb_id"] == 680
    request = route.calls.last.request
    assert request.url.params["with_genres"] == "80"
    assert request.url.params["primary_release_year"] == "1994"
    assert request.url.params["sort_by"] == "vote_average.desc"
    assert request.url.params["vote_count.gte"] == "200"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_discover_and_multi_search_return_pagination(tmp_path):
    discover_route = respx.get("https://api.themoviedb.org/3/discover/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 3,
                "total_pages": 9,
                "total_results": 171,
                "results": [{"id": 1399, "name": "权力的游戏"}],
            },
        )
    )
    respx.get("https://api.themoviedb.org/3/search/multi").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 2,
                "total_pages": 4,
                "total_results": 73,
                "results": [{"id": 1399, "media_type": "tv", "name": "权力的游戏"}],
            },
        )
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    discover = await client.get(
        "/api/v1/media/discover",
        params={"media_type": "tv", "genre_id": 10765, "page": 3},
    )
    search = await client.get(
        "/api/v1/media/search", params={"query": "权力的游戏", "page": 2}
    )

    assert discover.status_code == 200
    assert discover.json()["page"] == 3
    assert discover.json()["total_pages"] == 9
    assert discover.json()["results"][0]["media_type"] == "tv"
    assert discover_route.calls.last.request.url.params["with_genres"] == "10765"
    assert search.status_code == 200
    assert search.json()["page"] == 2
    assert search.json()["results"][0]["media_type"] == "tv"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_resource_search_falls_back_to_title_without_year(tmp_path):
    respx.get("https://api.themoviedb.org/3/tv/1399").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1399,
                "name": "权力的游戏",
                "original_name": "Game of Thrones",
                "first_air_date": "2011-04-17",
            },
        )
    )

    def pansou_response(request: httpx.Request) -> httpx.Response:
        query = request.url.params["kw"]
        if query == "权力的游戏":
            response = _pansou_response()
            response["data"]["merged_by_type"]["magnet"][0]["note"] = (
                "权力的游戏 S08 2019 2160p"
            )
            response["data"]["merged_by_type"]["115"][0]["note"] = (
                "权力的游戏 全8季 BluRay"
            )
            return httpx.Response(200, json=response)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"total": 0}},
        )

    route = respx.get("http://pansou.test/api/search").mock(side_effect=pansou_response)
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": 1399, "media_type": "tv"},
    )

    assert response.status_code == 200
    assert response.json()["movie"]["media_type"] == "tv"
    assert len(response.json()["results"]) == 2
    queries = {call.request.url.params["kw"] for call in route.calls}
    assert queries == {
        "权力的游戏",
        "权力的游戏 2011",
        "Game of Thrones",
        "Game of Thrones 2011",
    }
    async with database.session_factory() as session:
        cache = await session.scalar(select(SearchCache))
    assert cache.cache_key == "tmdb:tv:1399:queries:v4"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_season_search_filters_other_seasons_and_is_cache_isolated(tmp_path):
    tv_id = 1399
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tv_id,
                "name": "权力的游戏",
                "original_name": "Game of Thrones",
                "first_air_date": "2011-04-17",
                "seasons": [
                    {
                        "season_number": 2,
                        "name": "Season 2",
                        "episode_count": 10,
                        "air_date": "2012-04-01",
                        "poster_path": "/s2.jpg",
                    }
                ],
            },
        )
    )
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}/alternative_titles").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    def pansou_response(request: httpx.Request) -> httpx.Response:
        query = request.url.params["kw"]
        if query == "权力的游戏 第2季":
            entries = [
                ("1111111111111111111111111111111111111111", "S02E01"),
                ("2222222222222222222222222222222222222222", "S01E01"),
                ("3333333333333333333333333333333333333333", "S03E01"),
            ]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 3,
                        "merged_by_type": {
                            "magnet": [
                                {
                                    "url": f"magnet:?xt=urn:btih:{infohash}",
                                    "note": f"权力的游戏 {marker} 2012 1080p",
                                    "source": "plugin:season",
                                }
                                for infohash, marker in entries
                            ]
                        },
                    },
                },
            )
        return httpx.Response(200, json=_empty_pansou_response())

    route = respx.get("http://pansou.test/api/search").mock(side_effect=pansou_response)
    client, database, tmdb, pansou = await _make_client(tmp_path)

    selected = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 2},
    )
    all_seasons = await client.post(
        "/api/v1/search", json={"tmdb_id": tv_id, "media_type": "tv"}
    )

    assert selected.status_code == 200
    assert selected.json()["selected_season"] == 2
    assert len(selected.json()["results"]) == 1
    assert "S02" in selected.json()["results"][0]["name"]
    assert all_seasons.status_code == 200
    assert all_seasons.json()["selected_season"] is None
    assert {call.request.url.params["kw"] for call in route.calls} == {
        "权力的游戏",
        "权力的游戏 2011",
        "Game of Thrones",
        "Game of Thrones 2011",
        "权力的游戏 第2季",
        "权力的游戏 S02",
        "Game of Thrones Season 2",
        "Game of Thrones S02",
    }
    async with database.session_factory() as session:
        keys = {item.cache_key for item in await session.scalars(select(SearchCache))}
    assert keys == {
        "tmdb:tv:1399:queries:v4",
        "tmdb:tv:1399:season:2:queries:v4",
    }
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_missing_tv_season_returns_422_without_pansou_or_cache(tmp_path):
    tv_id = 1401
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tv_id,
                "name": "示例剧",
                "first_air_date": "2020-01-01",
                "seasons": [{"season_number": 1, "name": "Season 1"}],
            },
        )
    )
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 2},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "season_not_found"
    assert not pansou_route.called
    async with database.session_factory() as session:
        assert await session.scalar(select(SearchCache)) is None
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_season_zero_requires_tmdb_specials_and_can_search(tmp_path):
    tv_id = 1402
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tv_id,
                "name": "示例剧",
                "first_air_date": "2020-01-01",
                "seasons": [
                    {"season_number": 0, "name": "Specials"},
                ],
            },
        )
    )
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}/alternative_titles").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 0},
    )

    assert response.status_code == 200
    assert response.json()["selected_season"] == 0
    assert pansou_route.call_count == 4
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_tv_season_zero_is_rejected_when_tmdb_has_no_specials(tmp_path):
    tv_id = 1403
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tv_id,
                "name": "示例剧",
                "first_air_date": "2020-01-01",
                "seasons": [{"season_number": 1, "name": "Season 1"}],
            },
        )
    )
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 0},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "season_not_found"
    assert not pansou_route.called
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_cache_snapshot_preserves_context_order_and_scores(tmp_path):
    tv_id = 1600
    respx.get(f"https://api.themoviedb.org/3/tv/{tv_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tv_id,
                "name": "Context Show",
                "original_name": "Context Show",
                "first_air_date": "2011-01-01",
                "seasons": [
                    {"season_number": 2, "name": "Season 2"},
                    {"season_number": 3, "name": "Season 3"},
                ],
            },
        )
    )
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    now = datetime.now(UTC)
    resources = [
        Resource(
            id="res_shared",
            kind="magnet",
            canonical_key="magnet:shared",
            encrypted_url="cipher-shared",
            name="Shared",
            source="test",
            captured_at=now,
            expires_at=now + timedelta(days=7),
            metadata_json=json.dumps(
                {"rank_score": 5, "relevance_score": 5, "completeness_score": 5}
            ),
        ),
        Resource(
            id="res_other",
            kind="magnet",
            canonical_key="magnet:other",
            encrypted_url="cipher-other",
            name="Other",
            source="test",
            captured_at=now,
            expires_at=now + timedelta(days=7),
            metadata_json=json.dumps(
                {"rank_score": 95, "relevance_score": 95, "completeness_score": 95}
            ),
        ),
    ]
    cache_a = SearchCache(
        cache_key=f"tmdb:tv:{tv_id}:season:2:queries:v4",
        resource_ids_json=json.dumps(
            {
                "version": 1,
                "resources": [
                    {
                        "resource_id": "res_shared",
                        "rank_score": 90,
                        "relevance_score": 80,
                        "completeness_score": 70,
                    },
                    {
                        "resource_id": "res_other",
                        "rank_score": 60,
                        "relevance_score": 50,
                        "completeness_score": 40,
                    },
                ],
            }
        ),
        fetched_at=now,
        expires_at=now + timedelta(days=7),
    )
    cache_b = SearchCache(
        cache_key=f"tmdb:tv:{tv_id}:season:3:queries:v4",
        resource_ids_json=json.dumps(
            {
                "version": 1,
                "resources": [
                    {
                        "resource_id": "res_other",
                        "rank_score": 95,
                        "relevance_score": 95,
                        "completeness_score": 95,
                    },
                    {
                        "resource_id": "res_shared",
                        "rank_score": 10,
                        "relevance_score": 10,
                        "completeness_score": 10,
                    },
                ],
            }
        ),
        fetched_at=now,
        expires_at=now + timedelta(days=7),
    )
    async with database.session_factory() as session:
        session.add_all([*resources, cache_a, cache_b])
        await session.commit()

    response_a = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 2},
    )
    response_b = await client.post(
        "/api/v1/search",
        json={"tmdb_id": tv_id, "media_type": "tv", "season_number": 3},
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert pansou_route.call_count == 0
    assert [item["resource_id"] for item in response_a.json()["results"]] == [
        "res_shared",
        "res_other",
    ]
    assert [
        (
            item["rank_score"],
            item["relevance_score"],
            item["completeness_score"],
        )
        for item in response_a.json()["results"]
    ] == [(90, 80, 70), (60, 50, 40)]
    assert [item["resource_id"] for item in response_b.json()["results"]] == [
        "res_other",
        "res_shared",
    ]
    assert [
        (
            item["rank_score"],
            item["relevance_score"],
            item["completeness_score"],
        )
        for item in response_b.json()["results"]
    ] == [(95, 95, 95), (10, 10, 10)]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_public_search_reads_legacy_list_cache_scores(tmp_path):
    _mock_tmdb()
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    now = datetime.now(UTC)
    resource = Resource(
        id="res_legacy_list",
        kind="magnet",
        canonical_key="magnet:legacy-list",
        encrypted_url="cipher-legacy",
        name="Legacy cached resource",
        source="test",
        captured_at=now,
        expires_at=now + timedelta(days=7),
        metadata_json=json.dumps(
            {"rank_score": 61, "relevance_score": 52, "completeness_score": 43}
        ),
    )
    cache = SearchCache(
        cache_key="tmdb:movie:12345:queries:v4",
        resource_ids_json=json.dumps([resource.id]),
        fetched_at=now,
        expires_at=now + timedelta(days=7),
    )
    async with database.session_factory() as session:
        session.add_all([resource, cache])
        await session.commit()

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    body = response.json()

    assert response.status_code == 200
    assert pansou_route.call_count == 0
    assert body["results"] == [
        {
            "resource_id": "res_legacy_list",
            "kind": "magnet",
            "name": "Legacy cached resource",
            "size_bytes": None,
            "seeders": None,
            "source": "test",
            "captured_at": body["results"][0]["captured_at"],
            "size_source": None,
            "seeders_source": None,
            "seeders_observed_at": None,
            "rank_score": 61,
            "relevance_score": 52,
            "completeness_score": 43,
        }
    ]
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_public_search_cache_hit_preserves_pansou_metadata_sources(tmp_path):
    _mock_tmdb()
    pansou_route = respx.get("http://pansou.test/api/search").mock(
        return_value=httpx.Response(200, json=_empty_pansou_response())
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    now = datetime.now(UTC)
    resource = Resource(
        id="res_pansou_metadata",
        kind="magnet",
        canonical_key="magnet:pansou-metadata",
        encrypted_url="cipher-pansou-metadata",
        name="Pansou cached resource",
        size_bytes=2 * 1024**3,
        seeders=17,
        source="plugin:nyaa",
        captured_at=now,
        expires_at=now + timedelta(days=7),
        metadata_json=json.dumps(
            {
                "size_source": "pansou",
                "seeders_source": "pansou",
                "seeders_observed_at": "2026-07-25T04:00:00+00:00",
            }
        ),
    )
    cache = SearchCache(
        cache_key="tmdb:movie:12345:queries:v4",
        resource_ids_json=json.dumps([resource.id]),
        fetched_at=now,
        expires_at=now + timedelta(days=7),
    )
    async with database.session_factory() as session:
        session.add_all([resource, cache])
        await session.commit()

    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    result = response.json()["results"][0]

    assert response.status_code == 200
    assert pansou_route.call_count == 0
    assert result["size_source"] == "pansou"
    assert result["seeders_source"] == "pansou"
    assert result["seeders_observed_at"] == "2026-07-25T04:00:00Z"
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_concurrent_warm_with_barrier_share_checks_avoids_sqlite_lock(
    tmp_path,
):
    def search_response(request: httpx.Request) -> httpx.Response:
        query = request.url.params["kw"]
        title = "Movie A" if query.startswith("Movie A") else "Movie B"
        infohash = "a" * 40 if title == "Movie A" else "b" * 40
        code = "a" if title == "Movie A" else "b"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "merged_by_type": {
                        "magnet": [
                            {
                                "url": f"magnet:?xt=urn:btih:{infohash}",
                                "note": f"{title} 2020 1080p",
                                "source": f"plugin:{code}",
                            }
                        ],
                        "115": [
                            {
                                "url": f"https://115.com/s/{code}",
                                "note": f"{title} 2020 BluRay",
                                "source": f"plugin:{code}",
                            }
                        ],
                    },
                },
            },
        )

    respx.get("http://pansou.test/api/search").mock(side_effect=search_response)

    barrier = asyncio.Barrier(2)
    entered_checks = 0

    async def barrier_link_check(_request: httpx.Request) -> httpx.Response:
        nonlocal entered_checks
        entered_checks += 1
        await asyncio.wait_for(barrier.wait(), timeout=0.3)
        return httpx.Response(200, json={"results": [{"state": "ok"}]})

    respx.post("http://pansou.test/api/check/links").mock(
        side_effect=barrier_link_check
    )
    client, database, tmdb, pansou = await _make_client(tmp_path)
    service = client._transport.app.state.search_service

    results = await asyncio.wait_for(
        asyncio.gather(
            service.warm_media(
                MovieMetadata(tmdb_id=1501, title="Movie A", release_year=2020)
            ),
            service.warm_media(
                MovieMetadata(tmdb_id=1502, title="Movie B", release_year=2020)
            ),
        ),
        timeout=1.0,
    )

    assert results == [True, True]
    assert entered_checks == 2
    async with database.session_factory() as session:
        caches = list(await session.scalars(select(SearchCache)))
    assert {item.cache_key for item in caches} == {
        "tmdb:movie:1501:queries:v4",
        "tmdb:movie:1502:queries:v4",
    }
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_movie_rejects_season_request(tmp_path):
    _mock_tmdb()
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "season_number": 1}
    )
    negative = await client.post(
        "/api/v1/search", json={"tmdb_id": 12345, "season_number": -1}
    )

    assert response.status_code == 422
    assert negative.status_code == 422
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@respx.mock
async def test_resource_search_merges_all_queries_and_keeps_richer_duplicate(tmp_path):
    _mock_tmdb()
    richer_magnet = f"{MAGNET}&dn=Inception.2010.2160p&tr=https://tracker.test/announce"
    second_magnet = "magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678"

    def pansou_response(request: httpx.Request) -> httpx.Response:
        query = request.url.params["kw"]
        if query == "盗梦空间":
            return httpx.Response(200, json={"code": 0, "data": {"total": 0}})
        if query == "盗梦空间 2010":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 1,
                        "merged_by_type": {
                            "magnet": [
                                {
                                    "url": MAGNET,
                                    "note": "Inception",
                                    "source": "plugin:zh-year",
                                }
                            ]
                        },
                    },
                },
            )
        if query == "Inception":
            raise httpx.ReadTimeout("one query failed")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "merged_by_type": {
                        "magnet": [
                            {
                                "url": richer_magnet,
                                "name": "Inception 2010 2160p BluRay",
                                "note": "Inception 2010 2160p",
                                "source": "plugin:en-year",
                                "size": "12 GB",
                                "seeders": 42,
                            },
                            {
                                "url": second_magnet,
                                "note": "Inception extras 2010",
                                "source": "plugin:en-year",
                            },
                        ]
                    },
                },
            },
        )

    route = respx.get("http://pansou.test/api/search").mock(side_effect=pansou_response)
    client, database, tmdb, pansou = await _make_client(tmp_path)

    response = await client.post(
        "/api/v1/search",
        json={"tmdb_id": 12345, "refresh": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["warnings"] == ["pansou_query_failed:3", "partial_upstream"]
    assert route.call_count == 4
    assert {call.request.url.params["kw"] for call in route.calls} == {
        "盗梦空间",
        "盗梦空间 2010",
        "Inception",
        "Inception 2010",
    }
    richer = next(item for item in body["results"] if item["seeders"] == 42)
    assert richer["name"] == "Inception 2010 2160p BluRay"
    assert richer["size_bytes"] == 12 * 1024**3
    assert richer["source"] == "plugin:en-year"

    async with database.session_factory() as session:
        stored = list(await session.scalars(select(Resource)))
    duplicate = next(item for item in stored if item.seeders == 42)
    metadata = json.loads(duplicate.metadata_json)
    assert metadata["search_queries"] == ["盗梦空间 2010", "Inception 2010"]
    assert metadata["sources"] == ["plugin:zh-year", "plugin:en-year"]
    async with database.session_factory() as session:
        assert await session.scalar(select(SearchCache)) is None
    await _close(client, database, tmdb, pansou)
