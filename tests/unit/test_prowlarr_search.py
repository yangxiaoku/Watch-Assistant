import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from watch_assistant.adapters.prowlarr import (
    ProwlarrClient,
    ProwlarrRelease,
    ProwlarrSearchResult,
)
from watch_assistant.services.normalize import normalize_prowlarr
from watch_assistant.services.search import SearchService


class _Pansou:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response or {"merged_by_type": {"magnet": []}}
        self.error = error
        self.started: asyncio.Event | None = None

    async def search(self, _query: str):
        if self.started is not None:
            self.started.set()
        if self.error is not None:
            raise self.error
        return self.response


class _Prowlarr:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response or ProwlarrSearchResult(())
        self.error = error
        self.started: asyncio.Event | None = None

    async def search(self, _query: str):
        if self.started is not None:
            self.started.set()
        if self.error is not None:
            raise self.error
        return self.response


class _ClosableProwlarr(_Prowlarr):
    def __init__(self):
        super().__init__(ProwlarrSearchResult(()))
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def search(self, _query: str):
        self.started.set()
        await self.release.wait()
        return self.response

    async def aclose(self):
        self.closed = True


def _service(pansou, prowlarr):
    service = SearchService.__new__(SearchService)
    service._pansou = pansou
    service._prowlarr = prowlarr
    service._pansou_limit = asyncio.Semaphore(2)
    service._pansou_timeout = 1
    service._prowlarr_limit = asyncio.Semaphore(2)
    service._prowlarr_state_lock = asyncio.Lock()
    service._prowlarr_usage = {}
    service._prowlarr_idle = {}
    service._share_domains = ("115.com",)
    return service


@pytest.mark.asyncio
async def test_prowlarr_client_uses_conservative_default_page_size():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[], request=request)

    transport_client = httpx.AsyncClient(
        base_url="https://prowlarr.fixture.invalid",
        transport=httpx.MockTransport(handler),
    )
    client = ProwlarrClient(
        "https://prowlarr.fixture.invalid",
        "fixture-only",
        client=transport_client,
    )
    try:
        result = await client.search("Movie")
    finally:
        await client.aclose()
        await transport_client.aclose()

    assert result.releases == ()
    assert len(requests) == 1
    assert requests[0].url.params["limit"] == "1"
    assert requests[0].url.params["offset"] == "0"


@pytest.mark.asyncio
async def test_sources_are_queried_in_parallel_and_one_failure_degrades():
    prowlarr_started = asyncio.Event()
    pansou = _Pansou()
    prowlarr = _Prowlarr(error=RuntimeError("unavailable"))
    pansou.started = prowlarr_started
    prowlarr.started = asyncio.Event()

    async def pansou_search(_query: str):
        pansou.started.set()
        await prowlarr.started.wait()
        return pansou.response

    pansou.search = pansou_search
    result = await asyncio.wait_for(
        _service(pansou, prowlarr)._query_sources(("Movie",)), timeout=1
    )

    successful_pansou, successful_prowlarr, warnings, complete = result
    assert len(successful_pansou) == 1
    assert successful_prowlarr == []
    assert warnings == ["prowlarr_query_failed:1"]
    assert complete is False


@pytest.mark.asyncio
async def test_prowlarr_results_are_kept_when_pansou_fails():
    pansou = _Pansou(error=RuntimeError("unavailable"))
    prowlarr = _Prowlarr(
        ProwlarrSearchResult(
            (
                ProwlarrRelease(
                    title="Movie",
                    magnet_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
                    info_hash="abcdef0123456789abcdef0123456789abcdef01",
                    size_bytes=None,
                    seeders=None,
                    indexer=None,
                    indexer_id=None,
                    guid=None,
                    publish_date=None,
                    protocol="torrent",
                ),
            )
        )
    )

    successful_pansou, successful_prowlarr, warnings, complete = (
        await _service(pansou, prowlarr)._query_sources(("Movie",))
    )

    assert successful_pansou == []
    assert len(successful_prowlarr) == 1
    assert warnings == ["pansou_query_failed:1"]
    assert complete is False


@pytest.mark.asyncio
async def test_truncated_prowlarr_results_propagate_as_partial_warning():
    pansou = _Pansou()
    prowlarr = _Prowlarr(ProwlarrSearchResult((), truncated=True))

    successful_pansou, successful_prowlarr, warnings, complete = (
        await _service(pansou, prowlarr)._query_sources(("Movie",))
    )

    assert successful_pansou == [("Movie", pansou.response)]
    assert len(successful_prowlarr) == 1
    assert warnings == ["prowlarr_results_truncated", "partial_upstream"]
    assert complete is False


@pytest.mark.asyncio
async def test_pansou_and_prowlarr_results_dedupe_by_infohash():
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    pansou = _Pansou(
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": f"magnet:?xt=urn:btih:{info_hash}",
                        "note": "Movie 2010 1080p",
                        "source": "plugin:pansou",
                    }
                ]
            }
        }
    )
    prowlarr = _Prowlarr(
        ProwlarrSearchResult(
            (
                ProwlarrRelease(
                    title="Movie 2010 1080p",
                    magnet_url=f"magnet:?xt=urn:btih:{info_hash}",
                    info_hash=info_hash,
                    size_bytes=1024,
                    seeders=4,
                    indexer="Indexer A",
                    indexer_id=1,
                    guid=None,
                    publish_date="2026-08-01T00:00:00Z",
                    protocol="torrent",
                ),
            )
        )
    )
    service = _service(pansou, prowlarr)
    results = await service._query_sources(("Movie",))

    normalized = service._normalize_results(results[0], datetime.now(UTC))
    normalized = service._merge_normalized_results(
        normalized,
        service._normalize_prowlarr_results(results[1], datetime.now(UTC)),
    )

    assert len(normalized) == 1
    assert normalized[0].canonical_key == f"magnet:{info_hash}"
    assert set(normalized[0].metadata["sources"]) == {"plugin:pansou", "prowlarr"}
    assert normalized[0].size_bytes == 1024
    assert normalized[0].seeders == 4


def test_prowlarr_indexer_name_is_not_persisted_as_raw_provenance():
    release = ProwlarrRelease(
        title="Movie 2010 1080p",
        magnet_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        info_hash="abcdef0123456789abcdef0123456789abcdef01",
        size_bytes=None,
        seeders=None,
        indexer="https://indexer.test/search?marker=source-secret",
        indexer_id=7,
        guid=None,
        publish_date=None,
        protocol="torrent",
    )

    resource = normalize_prowlarr([release])[0]

    assert "source-secret" not in str(resource.metadata)
    assert "prowlarr_indexer" not in resource.metadata
    assert resource.metadata["prowlarr_indexer_id"] == 7


@pytest.mark.asyncio
async def test_replacing_prowlarr_client_waits_for_inflight_search():
    previous = _ClosableProwlarr()
    service = _service(_Pansou(), previous)
    search = asyncio.create_task(service._query_prowlarr("Movie"))
    await previous.started.wait()

    replacement = _Prowlarr()
    replace = asyncio.create_task(service.replace_prowlarr_client(replacement))
    await asyncio.sleep(0)
    assert not replace.done()
    assert previous.closed is False

    previous.release.set()
    await search
    await replace

    assert previous.closed is True
    assert service._prowlarr is replacement
    assert service._prowlarr_usage == {}
    assert service._prowlarr_idle == {}


@pytest.mark.asyncio
async def test_replacing_prowlarr_client_cleans_up_after_cancellation():
    previous = _ClosableProwlarr()
    service = _service(_Pansou(), previous)
    search = asyncio.create_task(service._query_prowlarr("Movie"))
    await previous.started.wait()

    replacement = _Prowlarr()
    replace = asyncio.create_task(service.replace_prowlarr_client(replacement))
    await asyncio.sleep(0)
    replace.cancel()

    previous.release.set()
    await search
    with pytest.raises(asyncio.CancelledError):
        await replace

    assert previous.closed is True
    assert service._prowlarr is replacement
    assert service._prowlarr_usage == {}
    assert service._prowlarr_idle == {}
