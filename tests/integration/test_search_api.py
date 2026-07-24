from datetime import UTC, datetime
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
from watch_assistant.models import Resource, SearchCache

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
    assert all("share-secret" not in (item.encrypted_password or "") for item in resources)
    assert MAGNET not in cache.resource_ids_json
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
                canonical_key=(
                    "magnet:abcdef0123456789abcdef0123456789abcdef01"
                ),
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
    magnet = next(item for item in response.json()["results"] if item["kind"] == "magnet")
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
    for index, feed in enumerate(("popular", "now_playing", "upcoming", "top_rated")):
        respx.get(f"https://api.themoviedb.org/3/movie/{feed}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 100 + index,
                            "title": feed,
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
    assert set(response.json()) == {"popular", "now_playing", "upcoming", "top_rated"}
    assert response.json()["popular"][0]["backdrop_path"] == "/popular.jpg"
    assert response.json()["top_rated"][0]["genre_ids"] == [18]
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
