import asyncio
from dataclasses import asdict
from urllib.parse import parse_qs

import httpx
import pytest
import respx

import watch_assistant.adapters.qbittorrent as qbittorrent_module
from watch_assistant.adapters.qbittorrent import (
    InspectionStatus,
    QbittorrentClient,
    QbittorrentInspectionResult,
)

BASE_URL = "http://qbittorrent.test"


def _magnet(infohash: str, *, tracker: str = "https://tracker.invalid/announce") -> str:
    return f"magnet:?xt=urn:btih:{infohash}&tr={tracker}"


class FakeQbittorrent:
    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        metadata_received: bool = True,
        files: list[dict[str, object]] | None = None,
        delete_status: int = 200,
        version: str = "v5.0.0",
        state: str | None = None,
        info_delay: float = 0,
        add_delay: float = 0,
        add_release: asyncio.Event | None = None,
        delete_release: asyncio.Event | None = None,
        add_response_text: str = "Ok.",
        add_response_json: dict[str, object] | None = None,
        state_sequence: list[str] | None = None,
        downloaded: int | None = None,
        info_failure_after_add: bool = False,
    ) -> None:
        self.existing = {value.casefold() for value in (existing or set())}
        self.metadata_received = metadata_received
        self.files = files or [{"name": "Movie.mkv", "size": 100}]
        self.delete_status = delete_status
        self.version = version
        self.state = state
        self.info_delay = info_delay
        self.add_delay = add_delay
        self.add_release = add_release
        self.delete_release = delete_release
        self.add_response_text = add_response_text
        self.add_response_json = add_response_json
        self.state_sequence = state_sequence
        self.downloaded = downloaded
        self.info_failure_after_add = info_failure_after_add
        self.added: dict[str, str] = {}
        self.added_state_checks = 0
        self.add_calls = 0
        self.delete_calls = 0
        self.delete_tag_calls = 0
        self.remove_category_calls = 0
        self.files_calls = 0
        self.active_checks = 0
        self.max_active_checks = 0
        self.active_adds = 0
        self.max_active_adds = 0
        self.cookie_seen = False
        self.add_started = asyncio.Event()
        self.add_seen = asyncio.Event()
        self.delete_started = asyncio.Event()
        self.delete_seen = asyncio.Event()

    def install(self) -> None:
        respx.post(f"{BASE_URL}/api/v2/auth/login").mock(side_effect=self._login)
        respx.get(f"{BASE_URL}/api/v2/app/version").mock(side_effect=self._version)
        respx.get(f"{BASE_URL}/api/v2/torrents/info").mock(side_effect=self._info)
        respx.post(f"{BASE_URL}/api/v2/torrents/add").mock(side_effect=self._add)
        respx.get(f"{BASE_URL}/api/v2/torrents/files").mock(side_effect=self._files)
        respx.post(f"{BASE_URL}/api/v2/torrents/delete").mock(side_effect=self._delete)
        respx.post(f"{BASE_URL}/api/v2/torrents/deleteTags").mock(
            side_effect=self._delete_tags
        )
        respx.post(f"{BASE_URL}/api/v2/torrents/removeCategories").mock(
            side_effect=self._remove_categories
        )

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form == {"username": ["user"], "password": ["password"]}
        return httpx.Response(
            200,
            text="Ok.",
            headers={"set-cookie": "SID=test-session; Path=/; HttpOnly"},
        )

    def _version(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=self.version)

    async def _info(self, request: httpx.Request) -> httpx.Response:
        self.active_checks += 1
        self.max_active_checks = max(self.max_active_checks, self.active_checks)
        try:
            if self.info_delay:
                await asyncio.sleep(self.info_delay)
            self.cookie_seen = (
                self.cookie_seen
                or "SID=test-session" in request.headers.get("cookie", "")
            )
            infohash = request.url.params.get("hashes")
            tag = request.url.params.get("tag")
            if infohash:
                normalized = infohash.casefold()
                if normalized in self.existing:
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "hash": normalized,
                                "state": "downloading",
                                "tags": "personal",
                            }
                        ],
                    )
                marker = self.added.get(normalized)
                if marker:
                    if self.info_failure_after_add:
                        raise httpx.ReadTimeout("controlled info failure")
                    state = self.state or (
                        "stoppedDL" if self.metadata_received else "metaDL"
                    )
                    if self.state_sequence is not None:
                        state = self.state_sequence[
                            min(self.added_state_checks, len(self.state_sequence) - 1)
                        ]
                    self.added_state_checks += 1
                    row: dict[str, object] = {
                        "hash": normalized,
                        "state": state,
                        "tags": marker,
                    }
                    if self.downloaded is not None:
                        row["downloaded"] = self.downloaded
                    return httpx.Response(
                        200,
                        json=[row],
                    )
                return httpx.Response(200, json=[])
            if tag:
                return httpx.Response(
                    200,
                    json=[
                        {"hash": added_hash, "state": "stoppedDL", "tags": marker}
                        for added_hash, marker in self.added.items()
                        if marker == tag
                    ],
                )
            return httpx.Response(200, json=[])
        finally:
            self.active_checks -= 1

    async def _add(self, request: httpx.Request) -> httpx.Response:
        self.active_adds += 1
        self.max_active_adds = max(self.max_active_adds, self.active_adds)
        try:
            self.add_started.set()
            if self.add_delay:
                await asyncio.sleep(self.add_delay)
            if self.add_release is not None:
                await self.add_release.wait()
            form = parse_qs(request.content.decode())
            magnet = form["urls"][0]
            infohash = magnet.split("urn:btih:", 1)[1].split("&", 1)[0].casefold()
            marker = form["tags"][0]
            assert form["category"] == [marker]
            assert form["stopCondition"] == ["MetadataReceived"]
            self.add_calls += 1
            self.added[infohash] = marker
            self.add_seen.set()
            if self.add_response_json is not None:
                return httpx.Response(200, json=self.add_response_json)
            return httpx.Response(200, text=self.add_response_text)
        finally:
            self.active_adds -= 1

    def _files(self, request: httpx.Request) -> httpx.Response:
        self.files_calls += 1
        assert request.url.params.get("hash") in self.added
        return httpx.Response(200, json=self.files)

    async def _delete(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        infohash = form["hashes"][0]
        assert form["deleteFiles"] == ["true"]
        assert infohash in self.added
        self.delete_calls += 1
        self.delete_started.set()
        if self.delete_release is not None:
            await self.delete_release.wait()
        if self.delete_status >= 400:
            return httpx.Response(self.delete_status, text="Fails.")
        self.added.pop(infohash)
        self.delete_seen.set()
        return httpx.Response(200, text="Ok.")

    def _delete_tags(self, request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode())["tags"]
        self.delete_tag_calls += 1
        return httpx.Response(200, text="Ok.")

    def _remove_categories(self, request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode())["categories"]
        self.remove_category_calls += 1
        return httpx.Response(200, text="Ok.")


@respx.mock
async def test_login_failure_returns_stable_error_without_magnet_details():
    respx.post(f"{BASE_URL}/api/v2/auth/login").mock(
        return_value=httpx.Response(200, text="Fails.")
    )
    secret_magnet = _magnet("a" * 40, tracker="https://secret.invalid/private")
    client = QbittorrentClient(BASE_URL, "user", "wrong-password")

    results = await client.inspect([secret_magnet])
    await client.aclose()

    assert results[0].status == InspectionStatus.FAILED
    assert results[0].error_code == "authentication_failed"
    assert "secret" not in str(asdict(results[0]))


@respx.mock
async def test_login_accepts_qbittorrent_5_empty_204_response():
    respx.post(f"{BASE_URL}/api/v2/auth/login").mock(
        return_value=httpx.Response(
            204,
            headers={"set-cookie": "SID=test-session; Path=/; HttpOnly"},
        )
    )
    respx.get(f"{BASE_URL}/api/v2/app/version").mock(
        return_value=httpx.Response(200, text="v5.2.3")
    )
    client = QbittorrentClient(BASE_URL, "user", "password")

    await client.ensure_available()
    await client.aclose()


@respx.mock
async def test_inspects_batch_of_30_and_maintains_login_cookie():
    fake = FakeQbittorrent()
    fake.install()
    magnets = [_magnet(f"{index + 1:040x}") for index in range(30)]
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    results = await client.inspect(magnets)
    await client.aclose()

    assert len(results) == 30
    assert all(result.status == InspectionStatus.VERIFIED for result in results)
    assert fake.add_calls == 30
    assert fake.delete_calls == 30
    assert fake.cookie_seen is True


@respx.mock
async def test_add_accepts_qbittorrent_5_json_success_response():
    fake = FakeQbittorrent(
        add_response_json={
            "added_torrent_ids": ["a" * 40],
            "failure_count": 0,
            "pending_count": 0,
            "success_count": 1,
        }
    )
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("a" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.VERIFIED


@respx.mock
async def test_qbittorrent_5_startup_states_wait_for_metadata_stop():
    fake = FakeQbittorrent(
        state_sequence=["queuedDL", "metaDL", "checkingResumeData", "stoppedDL"]
    )
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("a" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.VERIFIED


@respx.mock
async def test_downloaded_bytes_fail_before_metadata_is_read():
    fake = FakeQbittorrent(state="queuedDL", downloaded=1)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("a" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "metadata_stop_failed"
    assert fake.files_calls == 0


@respx.mock
async def test_batch_marker_is_removed_after_all_items_finish():
    fake = FakeQbittorrent()
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    results = await client.inspect([_magnet("a" * 40), _magnet("b" * 40)])
    await client.aclose()

    assert all(result.status == InspectionStatus.VERIFIED for result in results)
    assert fake.delete_tag_calls == 1
    assert fake.remove_category_calls == 1


@respx.mock
async def test_qbittorrent_4_4_5_is_rejected_before_add():
    fake = FakeQbittorrent(version="v4.4.5")
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("9" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "incompatible_qbittorrent"
    assert fake.add_calls == 0


@pytest.mark.parametrize("version", ["v4.5.0", "v5.0.0", "v5.1.2"])
@respx.mock
async def test_supported_qbittorrent_versions_can_inspect(version: str):
    fake = FakeQbittorrent(version=version)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("8" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.VERIFIED
    assert fake.add_calls == 1


@respx.mock
async def test_unavailable_version_endpoint_is_incompatible_before_add():
    fake = FakeQbittorrent()
    fake.install()
    respx.get(f"{BASE_URL}/api/v2/app/version").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("7" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "incompatible_qbittorrent"
    assert fake.add_calls == 0


@respx.mock
async def test_unparseable_version_is_incompatible_before_add():
    fake = FakeQbittorrent(version="qBittorrent development build")
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("0" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "incompatible_qbittorrent"
    assert fake.add_calls == 0


@respx.mock
async def test_release_candidate_version_is_incompatible_before_add():
    fake = FakeQbittorrent(version="v4.5.0-rc1")
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("3" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "incompatible_qbittorrent"
    assert fake.add_calls == 0


@respx.mock
async def test_overlong_version_returns_incompatible_without_leaking_value_error():
    fake = FakeQbittorrent(version=f"v{'9' * 10_000}.0.0")
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("2" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "incompatible_qbittorrent"
    assert fake.add_calls == 0


@respx.mock
async def test_instance_semaphore_limits_concurrent_batches():
    fake = FakeQbittorrent(info_delay=0.01)
    fake.install()
    client = QbittorrentClient(
        BASE_URL, "user", "password", concurrency=2, poll_interval=0
    )

    await asyncio.gather(
        client.inspect([_magnet(f"{index:040x}") for index in range(1, 4)]),
        client.inspect([_magnet(f"{index:040x}") for index in range(4, 7)]),
    )
    await client.aclose()

    assert fake.max_active_checks <= 2
    assert fake.max_active_checks == 2


async def test_eight_inspection_tasks_enter_concurrency_zone_together(
    monkeypatch: pytest.MonkeyPatch,
):
    client = QbittorrentClient(BASE_URL, "user", "password", concurrency=8)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_login() -> None:
        return None

    async def fake_inspect_one(item, _marker, _marker_used):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 8:
            entered.set()
        await release.wait()
        active -= 1
        return QbittorrentInspectionResult(
            infohash=item.infohash,
            status=InspectionStatus.VERIFIED,
        )

    monkeypatch.setattr(client, "_login", fake_login)
    monkeypatch.setattr(client, "_inspect_one", fake_inspect_one)
    task = asyncio.create_task(
        client.inspect([_magnet(f"{index:040x}") for index in range(1, 9)])
    )

    await asyncio.wait_for(entered.wait(), timeout=1)
    assert active == 8
    assert max_active == 8
    release.set()
    results = await task
    await client.aclose()

    assert len(results) == 8
    assert all(result.status == InspectionStatus.VERIFIED for result in results)


async def test_inspection_concurrency_cap_is_strict_with_controlled_barrier(
    monkeypatch: pytest.MonkeyPatch,
):
    client = QbittorrentClient(BASE_URL, "user", "password", concurrency=2)
    first_wave = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_login() -> None:
        return None

    async def fake_inspect_one(item, _marker, _marker_used):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            first_wave.set()
        await release.wait()
        active -= 1
        return QbittorrentInspectionResult(
            infohash=item.infohash,
            status=InspectionStatus.VERIFIED,
        )

    monkeypatch.setattr(client, "_login", fake_login)
    monkeypatch.setattr(client, "_inspect_one", fake_inspect_one)
    task = asyncio.create_task(
        client.inspect([_magnet(f"{index:040x}") for index in range(1, 5)])
    )

    await asyncio.wait_for(first_wave.wait(), timeout=1)
    assert active == 2
    assert max_active == 2
    release.set()
    await task
    await client.aclose()

    assert max_active == 2


@respx.mock
async def test_same_hash_is_serialized_across_batches_and_lock_table_is_reclaimed():
    fake = FakeQbittorrent(add_delay=0.01)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)
    magnet = _magnet("6" * 40)

    results = await asyncio.gather(client.inspect([magnet]), client.inspect([magnet]))
    await client.aclose()

    assert [batch[0].status for batch in results] == [
        InspectionStatus.VERIFIED,
        InspectionStatus.VERIFIED,
    ]
    assert fake.max_active_adds == 1
    assert fake.delete_calls == 2
    assert client._hash_locks == {}


@respx.mock
async def test_cancelled_before_add_response_still_cleans_added_torrent():
    add_release = asyncio.Event()
    fake = FakeQbittorrent(metadata_received=False, add_release=add_release)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)
    task = asyncio.create_task(client.inspect([_magnet("2" * 40)]))

    await asyncio.wait_for(fake.add_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    add_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()

    assert fake.add_calls == 1
    assert fake.delete_calls == 1
    assert fake.added == {}
    assert client._hash_locks == {}


@respx.mock
async def test_cancelled_inspect_finishes_shielded_cleanup_and_reraises():
    fake = FakeQbittorrent(metadata_received=False)
    fake.install()
    client = QbittorrentClient(
        BASE_URL, "user", "password", item_timeout=60, poll_interval=0.01
    )
    task = asyncio.create_task(client.inspect([_magnet("5" * 40)]))

    await asyncio.wait_for(fake.add_seen.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()

    assert fake.delete_seen.is_set()
    assert fake.added == {}
    assert client._hash_locks == {}


@respx.mock
async def test_repeated_cancel_during_cleanup_releases_hash_lock_table():
    delete_release = asyncio.Event()
    fake = FakeQbittorrent(delete_release=delete_release)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)
    task = asyncio.create_task(client.inspect([_magnet("1" * 40)]))

    await asyncio.wait_for(fake.delete_started.wait(), timeout=1)
    task.cancel()
    task.cancel()
    delete_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()

    assert fake.added == {}
    assert client._hash_locks == {}


@pytest.mark.parametrize(
    "state", ["downloading", "forcedDL", "error", "missingFiles", "unknown"]
)
@respx.mock
async def test_non_stopped_states_fail_without_reading_files(state: str):
    fake = FakeQbittorrent(state=state)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("4" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "metadata_stop_failed"
    assert fake.files_calls == 0


@respx.mock
async def test_rejects_more_than_30_without_calling_qbittorrent():
    client = QbittorrentClient(BASE_URL, "user", "password")

    with pytest.raises(ValueError, match="at most 30"):
        await client.inspect([_magnet(f"{index + 1:040x}") for index in range(31)])
    await client.aclose()

    assert not respx.calls.called


@respx.mock
async def test_duplicate_infohash_is_inspected_once():
    fake = FakeQbittorrent()
    fake.install()
    infohash = "b" * 40
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    results = await client.inspect(
        [_magnet(infohash), _magnet(infohash.upper(), tracker="udp://other.invalid")]
    )
    await client.aclose()

    assert [result.infohash for result in results] == [infohash]
    assert fake.add_calls == 1
    assert fake.delete_calls == 1


@respx.mock
async def test_metadata_timeout_still_cleans_owned_torrent():
    fake = FakeQbittorrent(metadata_received=False)
    fake.install()
    client = QbittorrentClient(
        BASE_URL, "user", "password", item_timeout=0, poll_interval=0
    )

    result = (await client.inspect([_magnet("c" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.TIMEOUT
    assert result.error_code == "metadata_timeout"
    assert fake.delete_calls == 1
    assert fake.delete_tag_calls == 1
    assert fake.remove_category_calls == 1


@respx.mock
async def test_thirty_second_metadata_timeout_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
):
    class ControlledClock:
        def __init__(self) -> None:
            self.values = iter((0.0, 31.0))

        def monotonic(self) -> float:
            return next(self.values)

    fake = FakeQbittorrent(metadata_received=False)
    fake.install()
    monkeypatch.setattr(qbittorrent_module, "time", ControlledClock())
    client = QbittorrentClient(
        BASE_URL,
        "user",
        "password",
        item_timeout=30,
        poll_interval=0,
    )

    result = (await client.inspect([_magnet("a" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.TIMEOUT
    assert result.error_code == "metadata_timeout"
    assert fake.delete_calls == 1
    assert fake.delete_tag_calls == 1
    assert fake.remove_category_calls == 1


@respx.mock
async def test_exception_after_add_cleans_torrent_tag_and_category():
    fake = FakeQbittorrent(info_failure_after_add=True)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("b" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "api_unavailable"
    assert fake.delete_calls == 1
    assert fake.delete_tag_calls == 1
    assert fake.remove_category_calls == 1


@respx.mock
async def test_classifies_mixed_files_and_returns_only_largest_basename():
    fake = FakeQbittorrent(
        files=[
            {"name": "Release/Feature/Movie.MKV", "size": 1_000},
            {"name": "Release/Extras/SAMPLE.mp4", "size": 100},
            {"name": "Release/Subs/Movie.ass", "size": 10},
            {"name": "Release/Subs/Movie.SRT", "size": 11},
            {"name": "Release/Proof/proof.avi", "size": 50},
            {"name": "Release/Tools/setup.EXE", "size": 20},
            {"name": "Release/Extras/trailer.mov", "size": 40},
            {"name": "Release/readme.txt", "size": 5},
        ]
    )
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("d" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.VERIFIED
    assert result.total_size_bytes == 1_236
    assert result.file_count == 8
    assert result.video_file_count == 4
    assert result.video_size_bytes == 1_190
    assert result.subtitle_count == 2
    assert result.sample_count == 4
    assert result.largest_video_name == "Movie.MKV"
    assert result.content_summary == "4 video(s), 2 subtitle(s), 4 suspicious file(s)"


@respx.mock
async def test_preexisting_torrent_is_never_added_or_deleted():
    infohash = "e" * 40
    fake = FakeQbittorrent(existing={infohash})
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet(infohash)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.UNSUPPORTED
    assert result.error_code == "existing_torrent"
    assert fake.add_calls == 0
    assert fake.delete_calls == 0


@respx.mock
async def test_cleanup_failure_discards_untrusted_metadata():
    fake = FakeQbittorrent(delete_status=500)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("f" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "cleanup_failed"
    assert result.file_count == 0
    assert fake.delete_calls == 1
    assert fake.delete_tag_calls == 0
    assert fake.remove_category_calls == 0


@respx.mock
async def test_malformed_api_response_returns_stable_error():
    respx.post(f"{BASE_URL}/api/v2/auth/login").mock(
        return_value=httpx.Response(
            200,
            text="Ok.",
            headers={"set-cookie": "SID=test-session; Path=/"},
        )
    )
    respx.get(f"{BASE_URL}/api/v2/app/version").mock(
        return_value=httpx.Response(200, text="v5.0.0")
    )
    respx.get(f"{BASE_URL}/api/v2/torrents/info").mock(
        return_value=httpx.Response(200, json={"unexpected": "object"})
    )
    respx.post(f"{BASE_URL}/api/v2/torrents/deleteTags").mock(
        return_value=httpx.Response(200, text="Ok.")
    )
    respx.post(f"{BASE_URL}/api/v2/torrents/removeCategories").mock(
        return_value=httpx.Response(200, text="Ok.")
    )
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("1" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "malformed_response"
