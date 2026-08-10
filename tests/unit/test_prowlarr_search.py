import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from watch_assistant.adapters.prowlarr import (
    ProwlarrClient,
    ProwlarrInvalidResponseError,
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
        self.kwargs: list[dict] = []

    async def search(self, _query: str, **kwargs):
        self.kwargs.append(kwargs)
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

    async def search(self, _query: str, **kwargs):
        self.kwargs.append(kwargs)
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
async def test_prowlarr_client_default_page_size_yields_expected_total_cap():
    # L5:默认每页 25 条 × 最多 20 页 = 500 条 = _MAX_RESULTS 总上限,
    # 而不是旧默认 1 条/页导致 CLI 等调用方最多只能拿到 20 条。
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
    assert requests[0].url.params["limit"] == "25"
    assert requests[0].url.params["offset"] == "0"


@pytest.mark.asyncio
async def test_prowlarr_client_default_pagination_total_cap_stays_at_max_results():
    # L5:默认分页下总条数上限仍是 _MAX_RESULTS(500):25 条/页 × 20 页
    # 恰好等于上限,分页循环不会越过剩余量继续请求。
    served = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        page = int(request.url.params.get("offset", "0"))
        items = [
            {
                "title": f"Movie {page + index:04d}",
                "protocol": "torrent",
                "magnetUrl": (
                    f"magnet:?xt=urn:btih:{page * 25 + index + 1:040x}&dn=Movie"
                ),
            }
            for index in range(25)
        ]
        return httpx.Response(200, json=items, request=request)

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

    # 20 页 × 25 条 = 500 条,即 _MAX_RESULTS 上限,不再继续请求
    assert len(result.releases) == 500
    assert served == 20


@pytest.mark.asyncio
async def test_large_single_page_under_max_results_is_accepted():
    # Prowlarr merges results across every queried indexer and over-fetches, so
    # a single page can legitimately exceed the per-request page-size cap (e.g.
    # 1337x ~80 plus YTS ~45 in one response). Only pages beyond the overall
    # result bound should be rejected as invalid.
    items = [
        {
            "title": f"Movie {index:03d}",
            "protocol": "torrent",
            "magnetUrl": f"magnet:?xt=urn:btih:{index:040x}&dn=Movie",
        }
        for index in range(1, 130)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        page = items[offset:]
        return httpx.Response(200, json=page, request=request)

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

    assert len(result.releases) == 129


@pytest.mark.asyncio
async def test_oversized_single_page_is_rejected():
    # A page that exceeds the overall result bound is still rejected as an
    # invalid response shape.
    items = [
        {
            "title": f"Movie {index:03d}",
            "protocol": "torrent",
            "magnetUrl": f"magnet:?xt=urn:btih:{index:040x}&dn=Movie",
        }
        for index in range(1, 502)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=items, request=request)

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
        with pytest.raises(ProwlarrInvalidResponseError):
            await client.search("Movie")
    finally:
        await client.aclose()
        await transport_client.aclose()


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


@pytest.mark.asyncio
async def test_download_url_release_is_resolved_to_magnet():
    infohash = "deadbeef0123456789deadbeef0123456789abcd"
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn=Movie"
    requested_download = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_download
        if "api/v1/search" in str(request.url):
            if request.url.params.get("offset") == "1":
                return httpx.Response(200, json=[], request=request)
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Movie 2010 1080p",
                        "protocol": "torrent",
                        "size": 1024,
                        "seeders": 4,
                        "indexer": "1337x",
                        "indexerId": 10,
                        "guid": "https://www.1377x.to/torrent/1/",
                        "downloadUrl": "https://prowlarr.fixture.invalid/10/download?link=abc",
                    }
                ],
                request=request,
            )
        requested_download = True
        return httpx.Response(
            302,
            headers={"location": magnet},
            request=request,
        )

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

    assert requested_download is True
    assert len(result.releases) == 1
    release = result.releases[0]
    assert release.magnet_url == magnet
    assert release.info_hash == infohash
    assert release.download_url is None


@pytest.mark.asyncio
async def test_download_url_release_is_dropped_when_no_magnet():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "api/v1/search" in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Movie 2010 1080p",
                        "protocol": "torrent",
                        "downloadUrl": "https://prowlarr.fixture.invalid/10/download?link=abc",
                    }
                ],
                request=request,
            )
        return httpx.Response(302, headers={"location": "https://example.com/file.torrent"}, request=request)

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


@pytest.mark.asyncio
async def test_query_prowlarr_forwards_indexer_ids():
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
    service = _service(_Pansou(), prowlarr)
    result = await service._query_prowlarr(
        "Movie", indexer_ids=(11, 16, 17)
    )
    assert len(result.releases) == 1
    assert prowlarr.kwargs[-1]["indexer_ids"] == (11, 16, 17)
    assert prowlarr.kwargs[-1]["limit"] == 50


@pytest.mark.asyncio
async def test_query_prowlarr_defaults_to_all_indexers():
    prowlarr = _Prowlarr()
    service = _service(_Pansou(), prowlarr)
    await service._query_prowlarr("Movie")
    kwargs = prowlarr.kwargs[-1]
    assert kwargs["indexer_ids"] is None
    assert kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_query_sources_threads_indexer_ids():
    prowlarr = _Prowlarr()
    service = _service(_Pansou(), prowlarr)
    await service._query_sources(("Movie",), indexer_ids=(11,))
    assert prowlarr.kwargs[-1]["indexer_ids"] == (11,)


@pytest.mark.asyncio
async def test_query_prowlarr_only_collects_failures():
    prowlarr = _Prowlarr()
    success = ProwlarrSearchResult(
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
    prowlarr.response = success

    async def failing_search(_query: str, **kwargs):
        prowlarr.kwargs.append(kwargs)
        raise RuntimeError("unavailable")

    async def ok_search(_query: str, **kwargs):
        prowlarr.kwargs.append(kwargs)
        return success

    prowlarr.search = failing_search
    service = _service(_Pansou(), prowlarr)
    successful, warnings, complete = await service._query_prowlarr_only(
        ("Movie",), indexer_ids=(10, 18)
    )
    assert successful == []
    assert warnings == ["prowlarr_query_failed:1"]
    assert complete is False

    prowlarr.search = ok_search
    successful, warnings, complete = await service._query_prowlarr_only(
        ("Movie",), indexer_ids=(10, 18)
    )
    assert len(successful) == 1
    assert warnings == []
    assert complete is True


@pytest.mark.asyncio
async def test_schedule_slow_merge_is_idempotent_per_cache_key():
    prowlarr = _Prowlarr()
    service = _service(_Pansou(), prowlarr)
    service._prowlarr_slow_ids = (10, 18)
    service._slow_merge_pending = set()
    created = []

    def fake_create_task(coro, *, name):
        created.append((coro, name))
        coro.close()
        return object()

    original_create_task = asyncio.create_task
    asyncio.create_task = fake_create_task
    try:
        service._schedule_slow_merge(
            _movie(), None, "movie:123", ["Movie"]
        )
        service._schedule_slow_merge(
            _movie(), None, "movie:123", ["Movie"]
        )
        service._schedule_slow_merge(
            _movie(), None, "movie:456", ["Movie"]
        )
    finally:
        asyncio.create_task = original_create_task
    assert len(created) == 2
    assert "movie:123" in service._slow_merge_pending
    assert "movie:456" in service._slow_merge_pending


def _movie():
    from watch_assistant.schemas import MediaType, MovieMetadata

    return MovieMetadata(
        tmdb_id=123,
        media_type=MediaType.MOVIE,
        title="Movie",
        original_title="Movie",
        release_year=2021,
    )
