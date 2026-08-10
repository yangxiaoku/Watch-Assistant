"""Integration tests for the fast/slow Prowlarr indexer split.

Real-time resource searches should only query the FAST indexers, and a
background task should later enrich the cache with SLOW-indexer results.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.prowlarr import ProwlarrClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import SearchCache
from watch_assistant.schemas import MediaType
from watch_assistant.services.search import make_cache_key

TMDB_RESPONSE = {
    "id": 438631,
    "title": "Dune",
    "original_title": "Dune",
    "release_date": "2021-10-22",
    "overview": "A desert planet.",
    "poster_path": "/dune.jpg",
}

FAST_IDS = (11, 16, 17)
SLOW_IDS = (10, 18)


def _release(title: str, indexer_id: int, infohash: str) -> dict:
    return {
        "title": title,
        "protocol": "torrent",
        "magnetUrl": f"magnet:?xt=urn:btih:{infohash}&dn={title}",
        "size": 1024,
        "seeders": 5,
        "indexerId": indexer_id,
        "indexer": f"indexer-{indexer_id}",
    }


class _SlowOrchestrator:
    """Records Prowlarr requests and can fail slow queries on demand."""

    def __init__(self) -> None:
        self.fast_queries: list[int] = []
        self.slow_queries: list[int] = []
        self.fail_slow = False
        self.slow_reached = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        ids = [int(v) for v in request.url.params.get_list("indexerIds")]
        if not ids:
            ids = list(FAST_IDS) + list(SLOW_IDS)
        # Determine whether this request targets only fast indexers.
        slow_only = set(ids) <= set(SLOW_IDS) and ids
        fast_only = set(ids) <= set(FAST_IDS) and ids
        if fast_only:
            self.fast_queries.append(sorted(ids))
            return httpx.Response(
                200,
                json=[
                    _release("Dune (2021) 1080p YTS", 11, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                    _release("Dune (2021) 1080p Nyaa", 16, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                ],
                request=request,
            )
        if slow_only:
            self.slow_queries.append(sorted(ids))
            self.slow_reached.set()
            if self.fail_slow:
                return httpx.Response(503, json=[], request=request)
            return httpx.Response(
                200,
                json=[
                    _release("Dune (2021) 1080p TPB", 18, "1111111111111111111111111111111111111111"),
                    _release("Dune (2021) 1080p 1337x", 10, "2222222222222222222222222222222222222222"),
                ],
                request=request,
            )
        # Mixed or unknown — return empty.
        return httpx.Response(200, json=[], request=request)


async def _make_client(tmp_path: Path, orchestrator: _SlowOrchestrator):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    await initialize_database(database.engine)

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/movie/438631/alternative_titles"):
            return httpx.Response(200, json={"titles": []}, request=request)
        if request.url.path.endswith("/movie/438631"):
            return httpx.Response(200, json=TMDB_RESPONSE, request=request)
        return httpx.Response(404, json={}, request=request)

    tmdb = TmdbClient(
        "tmdb-secret",
        client=httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3",
            transport=httpx.MockTransport(tmdb_handler),
        ),
    )
    def pansou_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"total": 0, "merged_by_type": {"magnet": []}},
            },
            request=request,
        )

    pansou = PanSouClient(
        "http://pansou.test",
        client=httpx.AsyncClient(
            base_url="http://pansou.test",
            transport=httpx.MockTransport(pansou_handler),
        ),
    )
    prowlarr = ProwlarrClient(
        "https://prowlarr.test",
        "fixture-only",
        client=httpx.AsyncClient(
            base_url="https://prowlarr.test",
            transport=httpx.MockTransport(orchestrator.handler),
        ),
    )
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        prowlarr_client=prowlarr,
        share_domains=("115.com",),
        prowlarr_fast_indexer_ids=FAST_IDS,
        prowlarr_slow_indexer_ids=SLOW_IDS,
    security_manager=make_security_manager(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, prowlarr


async def _close(client, database, tmdb, pansou, prowlarr):
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await prowlarr.aclose()
    await database.engine.dispose()


async def _search(client, tmdb_id: int) -> dict:
    response = await client.post(
        f"/api/v1/media/movie/{tmdb_id}/resource-search",
        json={"refresh": True},
    )
    assert response.status_code == 202, response.text
    task = response.json()
    for _ in range(40):
        poll = await client.get(f"/api/v1/resource-search/{task['task_id']}")
        state = poll.json()
        if state["status"] in {"ready", "failed"}:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("resource search did not finish")


@pytest.mark.asyncio
async def test_realtime_search_queries_only_fast_indexers(tmp_path: Path):
    orch = _SlowOrchestrator()
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path, orch)
    try:
        state = await _search(client, 438631)
        assert state["status"] == "ready", state
        assert all("prowlarr_query_failed" not in w for w in state["warnings"]), state["warnings"]
        # Fast indexers were queried in real-time; the search returned quickly
        # with no failures (had slow indexers been in the real-time path, the
        # query would time out and emit prowlarr_query_failed).
        assert orch.fast_queries, "expected at least one fast query"
    finally:
        await _close(client, database, tmdb, pansou, prowlarr)


@pytest.mark.asyncio
async def test_background_slow_merge_enriches_cache(tmp_path: Path):
    orch = _SlowOrchestrator()
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path, orch)
    try:
        await _search(client, 438631)
        # Wait for the background slow merge to reach Prowlarr and persist.
        await asyncio.wait_for(orch.slow_reached.wait(), timeout=5)
        for _ in range(50):
            async with database.session_factory() as session:
                cache = await session.get(
                    SearchCache, make_cache_key(438631, MediaType.MOVIE, None)
                )
            if cache is not None and cache.cache_kind == "positive":
                break
            await asyncio.sleep(0.1)
        assert cache is not None
        assert cache.cache_kind == "positive"
        snapshot = json.loads(cache.resource_ids_json)
        assert len(snapshot["resources"]) >= 2, snapshot
    finally:
        await _close(client, database, tmdb, pansou, prowlarr)


@pytest.mark.asyncio
async def test_slow_timeout_does_not_break_realtime_search(tmp_path: Path):
    orch = _SlowOrchestrator()
    orch.fail_slow = True
    client, database, tmdb, pansou, prowlarr = await _make_client(tmp_path, orch)
    try:
        state = await _search(client, 438631)
        assert state["status"] == "ready", state
        # Slow failures must not surface as real-time query failures.
        assert all("prowlarr_query_failed" not in w for w in state["warnings"]), state["warnings"]
        # Let the failing merge run; it must not raise.
        await asyncio.wait_for(orch.slow_reached.wait(), timeout=5)
        await asyncio.sleep(0.5)
    finally:
        await _close(client, database, tmdb, pansou, prowlarr)
