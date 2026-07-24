from dataclasses import asdict
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from watch_assistant.adapters.qbittorrent import (
    InspectionStatus,
    QbittorrentClient,
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
    ) -> None:
        self.existing = {value.casefold() for value in (existing or set())}
        self.metadata_received = metadata_received
        self.files = files or [{"name": "Movie.mkv", "size": 100}]
        self.delete_status = delete_status
        self.added: dict[str, str] = {}
        self.add_calls = 0
        self.delete_calls = 0
        self.cookie_seen = False

    def install(self) -> None:
        respx.post(f"{BASE_URL}/api/v2/auth/login").mock(side_effect=self._login)
        respx.get(f"{BASE_URL}/api/v2/torrents/info").mock(side_effect=self._info)
        respx.post(f"{BASE_URL}/api/v2/torrents/add").mock(side_effect=self._add)
        respx.get(f"{BASE_URL}/api/v2/torrents/files").mock(side_effect=self._files)
        respx.post(f"{BASE_URL}/api/v2/torrents/delete").mock(side_effect=self._delete)

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form == {"username": ["user"], "password": ["password"]}
        return httpx.Response(
            200,
            text="Ok.",
            headers={"set-cookie": "SID=test-session; Path=/; HttpOnly"},
        )

    def _info(self, request: httpx.Request) -> httpx.Response:
        self.cookie_seen = (
            self.cookie_seen or "SID=test-session" in request.headers.get("cookie", "")
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
                return httpx.Response(
                    200,
                    json=[
                        {
                            "hash": normalized,
                            "state": "stoppedDL"
                            if self.metadata_received
                            else "metaDL",
                            "tags": marker,
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        if tag:
            return httpx.Response(
                200,
                json=[
                    {"hash": infohash, "state": "stoppedDL", "tags": marker}
                    for infohash, marker in self.added.items()
                    if marker == tag
                ],
            )
        return httpx.Response(200, json=[])

    def _add(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        magnet = form["urls"][0]
        infohash = magnet.split("urn:btih:", 1)[1].split("&", 1)[0].casefold()
        marker = form["tags"][0]
        assert form["category"] == [marker]
        assert form["stopCondition"] == ["MetadataReceived"]
        self.add_calls += 1
        self.added[infohash] = marker
        return httpx.Response(200, text="Ok.")

    def _files(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("hash") in self.added
        return httpx.Response(200, json=self.files)

    def _delete(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        infohash = form["hashes"][0]
        assert form["deleteFiles"] == ["true"]
        assert infohash in self.added
        self.delete_calls += 1
        if self.delete_status >= 400:
            return httpx.Response(self.delete_status, text="Fails.")
        self.added.pop(infohash)
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
async def test_cleanup_failure_is_reported_without_discarding_metadata():
    fake = FakeQbittorrent(delete_status=500)
    fake.install()
    client = QbittorrentClient(BASE_URL, "user", "password", poll_interval=0)

    result = (await client.inspect([_magnet("f" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "cleanup_failed"
    assert result.file_count == 1
    assert fake.delete_calls == 1


@respx.mock
async def test_malformed_api_response_returns_stable_error():
    respx.post(f"{BASE_URL}/api/v2/auth/login").mock(
        return_value=httpx.Response(
            200,
            text="Ok.",
            headers={"set-cookie": "SID=test-session; Path=/"},
        )
    )
    respx.get(f"{BASE_URL}/api/v2/torrents/info").mock(
        return_value=httpx.Response(200, json={"unexpected": "object"})
    )
    client = QbittorrentClient(BASE_URL, "user", "password")

    result = (await client.inspect([_magnet("1" * 40)]))[0]
    await client.aclose()

    assert result.status == InspectionStatus.FAILED
    assert result.error_code == "malformed_response"
