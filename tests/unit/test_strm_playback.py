import asyncio
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest

from watch_assistant.adapters.p115_playback_contract import (
    DynamicLinkOutcome,
    PlaybackGate,
    PlaybackStatus,
    forwarding_policy,
    make_playback_request,
)
from watch_assistant.api import strm as strm_api
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)
from watch_assistant.services.strm_playback import (
    CachedStrmPlaybackGateway,
    validate_current_manifest_scope,
)

MANIFEST_ID = "strm_7qXh5Mptv9K2dR4a"
OTHER_MANIFEST_ID = "strm_8qXh5Mptv9K2dR4a"
SAFE_URL = "https://cdn.example.invalid/media?sig=redacted"
ENABLED_GATE = PlaybackGate(enabled=True, contract_verified=True)


async def _valid_cache(_request, _allowed_library_ids):
    return True


class _CountingGateway:
    def __init__(self, outcome: DynamicLinkOutcome):
        self.outcome = outcome
        self.calls = 0
        self.scopes = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def resolve(self, request, *, gate, allowed_library_ids=None):
        del request, gate
        self.calls += 1
        self.scopes.append(allowed_library_ids)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.outcome


@pytest.mark.asyncio
async def test_ready_link_is_cached_with_request_specific_head_range_policy():
    gateway = _CountingGateway(
        DynamicLinkOutcome(
            PlaybackStatus.READY,
            url=SAFE_URL,
            request_headers=(("User-Agent", "test-agent"),),
        )
    )
    resolver = CachedStrmPlaybackGateway(
        gateway, cache_validator=_valid_cache, ttl_seconds=30
    )

    get = await resolver.resolve(
        make_playback_request(MANIFEST_ID, "GET", "bytes=0-99"),
        gate=ENABLED_GATE,
        allowed_library_ids={"library-a"},
    )
    head = await resolver.resolve(
        make_playback_request(MANIFEST_ID, "HEAD", "bytes=0-99"),
        gate=ENABLED_GATE,
        allowed_library_ids={"library-a"},
    )

    assert gateway.calls == 1
    assert get.policy is not None and get.policy.forward_range is True
    assert head.policy is not None and head.policy.forward_range is False
    assert head.request_headers == (("User-Agent", "test-agent"),)
    assert resolver.cached_entry_count == 1


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_manifest_are_single_flight():
    gateway = _CountingGateway(DynamicLinkOutcome(PlaybackStatus.READY, url=SAFE_URL))
    gateway.release = asyncio.Event()
    resolver = CachedStrmPlaybackGateway(gateway, cache_validator=_valid_cache)
    request = make_playback_request(MANIFEST_ID, "GET")

    first = asyncio.create_task(
        resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-a"})
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=1)
    second = asyncio.create_task(
        resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-a"})
    )
    await asyncio.sleep(0)
    assert gateway.calls == 1

    gateway.release.set()
    results = await asyncio.gather(first, second)
    assert [result.status for result in results] == [
        PlaybackStatus.READY,
        PlaybackStatus.READY,
    ]
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_cache_expires_and_scope_isolation_prevents_cross_scope_reuse():
    now = 0.0

    def clock():
        return now

    gateway = _CountingGateway(DynamicLinkOutcome(PlaybackStatus.READY, url=SAFE_URL))
    resolver = CachedStrmPlaybackGateway(
        gateway, cache_validator=_valid_cache, ttl_seconds=5, clock=clock
    )
    request = make_playback_request(MANIFEST_ID, "GET")

    await resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-a"})
    await resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-b"})
    assert gateway.calls == 2

    now = 4.9
    await resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-a"})
    assert gateway.calls == 2

    now = 5.0
    await resolver.resolve(request, gate=ENABLED_GATE, allowed_library_ids={"library-a"})
    assert gateway.calls == 3


@pytest.mark.asyncio
async def test_cache_hit_rechecks_current_manifest_scope_before_reuse():
    valid = True
    validations = 0

    async def validator(_request, _allowed_library_ids):
        nonlocal validations
        validations += 1
        return valid

    gateway = _CountingGateway(DynamicLinkOutcome(PlaybackStatus.READY, url=SAFE_URL))
    resolver = CachedStrmPlaybackGateway(
        gateway, cache_validator=validator, ttl_seconds=30
    )
    request = make_playback_request(MANIFEST_ID, "GET")

    await resolver.resolve(request, gate=ENABLED_GATE)
    valid = False
    await resolver.resolve(request, gate=ENABLED_GATE)

    assert validations == 1
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_manifest_scope_validator_fails_closed_for_scope_and_revocation(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'playback.db'}")
    await initialize_database(database.engine)
    try:
        async with database.session_factory() as session:
            session.add(
                MediaLibrary(
                    id="library-one",
                    name="test-library",
                    root_directory_id="100",
                    scope_verified=True,
                    enabled=True,
                    revision=1,
                )
            )
            session.add(
                StrmManifestEntry(
                    manifest_id=MANIFEST_ID,
                    library_id="library-one",
                    cloud_file_id="200",
                    cloud_directory_id="100",
                    cloud_relative_path="Episode.mkv",
                    local_relative_path="Episode.strm",
                    size_bytes=10,
                    source_version=1,
                    status=StrmManifestStatus.VERIFIED,
                    is_current=True,
                )
            )
            await session.commit()

        request = make_playback_request(MANIFEST_ID, "GET")
        assert await validate_current_manifest_scope(
            database.session_factory, request, {"library-one"}
        )
        assert not await validate_current_manifest_scope(
            database.session_factory, request, {"library-two"}
        )

        async with database.session_factory() as session:
            manifest = await session.get(StrmManifestEntry, MANIFEST_ID)
            assert manifest is not None
            manifest.status = StrmManifestStatus.RETIRED
            await session.commit()
        assert not await validate_current_manifest_scope(
            database.session_factory, request, {"library-one"}
        )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_and_failed_outcomes_are_never_cached():
    gateway = _CountingGateway(
        DynamicLinkOutcome(PlaybackStatus.FAILED, error_code="remote_failed")
    )
    resolver = CachedStrmPlaybackGateway(gateway, cache_validator=_valid_cache)
    request = make_playback_request(MANIFEST_ID, "GET")

    disabled = await resolver.resolve(request, gate=PlaybackGate())
    disabled_again = await resolver.resolve(request, gate=PlaybackGate())
    assert disabled.status is PlaybackStatus.DISABLED
    assert disabled_again.status is PlaybackStatus.DISABLED
    assert gateway.calls == 0

    failed = await resolver.resolve(request, gate=ENABLED_GATE)
    failed_again = await resolver.resolve(request, gate=ENABLED_GATE)
    assert failed.status is PlaybackStatus.FAILED
    assert failed_again.status is PlaybackStatus.FAILED
    assert gateway.calls == 2
    assert resolver.cached_entry_count == 0


@pytest.mark.asyncio
async def test_cache_is_bounded():
    gateway = _CountingGateway(DynamicLinkOutcome(PlaybackStatus.READY, url=SAFE_URL))
    resolver = CachedStrmPlaybackGateway(
        gateway, cache_validator=_valid_cache, max_entries=1
    )

    await resolver.resolve(
        make_playback_request(MANIFEST_ID, "GET"), gate=ENABLED_GATE
    )
    await resolver.resolve(
        make_playback_request(OTHER_MANIFEST_ID, "GET"), gate=ENABLED_GATE
    )

    assert resolver.cached_entry_count == 1


def test_api_reuses_one_cached_resolver_for_each_gateway_instance():
    gateway = _CountingGateway(DynamicLinkOutcome(PlaybackStatus.READY, url=SAFE_URL))
    app = SimpleNamespace(state=SimpleNamespace(strm_playback_gateway=gateway))
    request = SimpleNamespace(app=app)

    first = strm_api._playback_gateway(request)
    second = strm_api._playback_gateway(request)

    assert isinstance(first, CachedStrmPlaybackGateway)
    assert first is second
    assert first.upstream is gateway


class _FakeUpstreamResponse:
    status_code = 206
    headers = httpx.Headers(
        {
            "accept-ranges": "bytes",
            "content-length": "3",
            "content-range": "bytes 0-2/3",
            "content-type": "video/x-matroska",
            "x-secret": "must-not-forward",
        }
    )

    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self):
        yield b"abc"


class _FakeHttpxClient:
    instances: ClassVar[list["_FakeHttpxClient"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.request = None
        self.upstream = _FakeUpstreamResponse()
        self.closed = False
        self.__class__.instances.append(self)

    def build_request(self, method, url, *, headers):
        self.request = (method, url, dict(headers))
        return self.request

    async def send(self, request, *, stream):
        assert request is self.request
        assert stream is True
        return self.upstream

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_forwards_get_range_but_never_head_range(monkeypatch):
    monkeypatch.setattr(strm_api.httpx, "AsyncClient", _FakeHttpxClient)
    _FakeHttpxClient.instances.clear()

    head = await strm_api._proxy_playback(
        None,
        make_playback_request(MANIFEST_ID, "HEAD", "bytes=0-2"),
        SAFE_URL,
        (("User-Agent", "test-agent"),),
    )
    head_client = _FakeHttpxClient.instances[-1]
    assert head_client.request[0] == "HEAD"
    assert "Range" not in head_client.request[2]
    assert head.status_code == 206

    get = await strm_api._proxy_playback(
        None,
        make_playback_request(MANIFEST_ID, "GET", "bytes=0-2"),
        SAFE_URL,
        (("User-Agent", "test-agent"),),
    )
    get_client = _FakeHttpxClient.instances[-1]
    assert get_client.request[0] == "GET"
    assert get_client.request[2]["Range"] == "bytes=0-2"
    assert get.status_code == 206
    assert b"".join([chunk async for chunk in get.body_iterator]) == b"abc"


@pytest.mark.asyncio
async def test_playback_redirects_without_range_and_preserves_range_proxy_contract(monkeypatch):
    monkeypatch.setattr(strm_api.httpx, "AsyncClient", _FakeHttpxClient)
    _FakeHttpxClient.instances.clear()

    head_request = make_playback_request(MANIFEST_ID, "HEAD")
    head = await strm_api._serve_playback(
        None,
        head_request,
        upstream_url=SAFE_URL,
        policy=forwarding_policy(head_request),
        upstream_headers=(("User-Agent", "test-agent"),),
    )
    assert head.status_code == 307
    assert head.headers["location"] == SAFE_URL
    assert _FakeHttpxClient.instances == []

    head_range_request = make_playback_request(MANIFEST_ID, "HEAD", "bytes=0-2")
    head_range = await strm_api._serve_playback(
        None,
        head_range_request,
        upstream_url=SAFE_URL,
        policy=forwarding_policy(head_range_request),
        upstream_headers=(("User-Agent", "test-agent"),),
    )
    head_range_client = _FakeHttpxClient.instances[-1]
    assert head_range.status_code == 206
    assert "Range" not in head_range_client.request[2]

    get_request = make_playback_request(MANIFEST_ID, "GET")
    get = await strm_api._serve_playback(
        None,
        get_request,
        upstream_url=SAFE_URL,
        policy=forwarding_policy(get_request),
        upstream_headers=(("User-Agent", "test-agent"),),
    )
    assert get.status_code == 307
    assert get.headers["location"] == SAFE_URL
    assert len(_FakeHttpxClient.instances) == 1

    ranged_request = make_playback_request(MANIFEST_ID, "GET", "bytes=0-2")
    ranged = await strm_api._serve_playback(
        None,
        ranged_request,
        upstream_url=SAFE_URL,
        policy=forwarding_policy(ranged_request),
        upstream_headers=(("User-Agent", "test-agent"),),
    )
    ranged_client = _FakeHttpxClient.instances[-1]
    assert ranged.status_code == 206
    assert ranged_client.request[2]["Range"] == "bytes=0-2"
    assert b"".join([chunk async for chunk in ranged.body_iterator]) == b"abc"
