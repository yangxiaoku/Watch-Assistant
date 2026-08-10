"""Isolated qBittorrent adapter for magnet metadata inspection."""

import asyncio
import base64
import binascii
import re
import time
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

# Public HTTP trackers that are commonly reachable even when UDP/DHT egress is
# restricted (e.g. behind the GFW).  PanSou releases are infohash-only or carry
# UDP trackers, so appending these lets qBittorrent fetch metadata over plain
# HTTP.  Kept as a small tuple so the magnet URL stays bounded.
_FALLBACK_HTTP_TRACKERS = (
    "http://tracker.opentrackr.org:1337/announce",
    "http://tracker.dler.org:6969/announce",
)

VIDEO_EXTENSIONS = {".avi", ".m2ts", ".mkv", ".mov", ".mp4", ".ts"}
SUBTITLE_EXTENSIONS = {".ass", ".srt", ".ssa", ".sub", ".vtt"}
SUSPICIOUS_EXTENSIONS = {".bat", ".cmd", ".com", ".exe", ".msi", ".scr"}
SUSPICIOUS_NAME = re.compile(
    r"(?:^|[/._\-\s])(sample|trailer|proof)(?:[/._\-\s]|$)", re.IGNORECASE
)
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
MAX_VERSION_COMPONENT_DIGITS = 3
MINIMUM_QBITTORRENT_VERSION = (4, 5, 0)


class InspectionStatus(StrEnum):
    VERIFIED = "verified"
    TIMEOUT = "timeout"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class QbittorrentInspectionResult:
    infohash: str | None
    status: InspectionStatus
    total_size_bytes: int = 0
    file_count: int = 0
    video_file_count: int = 0
    video_size_bytes: int = 0
    subtitle_count: int = 0
    sample_count: int = 0
    largest_video_name: str | None = None
    content_summary: str | None = None
    error_code: str | None = None
    result_source: str | None = None


@dataclass(frozen=True)
class _Magnet:
    uri: str
    infohash: str


@dataclass
class _HashLockState:
    lock: asyncio.Lock
    users: int = 0


class _ApiError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _UnsupportedError(_ApiError):
    pass


class QbittorrentClient:
    """Fetch torrent metadata without allowing payload downloads."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        concurrency: int = 4,
        item_timeout: float = 120.0,
        poll_interval: float = 1.0,
        request_timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if item_timeout < 0 or poll_interval < 0 or request_timeout <= 0:
            raise ValueError("timeouts must be non-negative")
        self._username = username
        self._password = password
        self._concurrency = concurrency
        self._item_timeout = item_timeout
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._hash_table_lock = asyncio.Lock()
        self._hash_locks: dict[str, _HashLockState] = {}

    async def inspect(
        self, magnets: Sequence[str]
    ) -> list[QbittorrentInspectionResult]:
        if len(magnets) > 30:
            raise ValueError("at most 30 magnets may be inspected")

        parsed: list[_Magnet] = []
        entries: list[_Magnet | QbittorrentInspectionResult] = []
        seen: set[str] = set()
        for uri in magnets:
            infohash = _extract_infohash(uri)
            if infohash is None:
                entries.append(
                    QbittorrentInspectionResult(
                        infohash=None,
                        status=InspectionStatus.UNSUPPORTED,
                        error_code="invalid_magnet",
                    )
                )
            elif infohash not in seen:
                seen.add(infohash)
                item = _Magnet(uri=uri, infohash=infohash)
                parsed.append(item)
                entries.append(item)

        if not parsed:
            return [entry for entry in entries if not isinstance(entry, _Magnet)]

        try:
            await self._login()
        except _UnsupportedError as exc:
            return [
                (
                    QbittorrentInspectionResult(
                        infohash=entry.infohash,
                        status=InspectionStatus.UNSUPPORTED,
                        error_code=exc.code,
                    )
                    if isinstance(entry, _Magnet)
                    else entry
                )
                for entry in entries
            ]
        except _ApiError as exc:
            return [
                (
                    QbittorrentInspectionResult(
                        infohash=entry.infohash,
                        status=InspectionStatus.FAILED,
                        error_code=exc.code,
                    )
                    if isinstance(entry, _Magnet)
                    else entry
                )
                for entry in entries
            ]

        batch_marker = f"wa-inspect-{uuid.uuid4().hex}"
        marker_used = asyncio.Event()

        async def run(item: _Magnet) -> QbittorrentInspectionResult:
            async with self._semaphore:
                return await self._inspect_one(item, batch_marker, marker_used)

        try:
            inspected = await asyncio.gather(*(run(item) for item in parsed))
        except asyncio.CancelledError:
            if marker_used.is_set():
                try:
                    await self._shielded_marker_cleanup(batch_marker)
                except _ApiError:
                    pass
            raise

        if marker_used.is_set():
            try:
                await self._shielded_marker_cleanup(batch_marker)
            except _ApiError:
                inspected = [
                    QbittorrentInspectionResult(
                        infohash=result.infohash,
                        status=InspectionStatus.FAILED,
                        error_code="cleanup_failed",
                    )
                    for result in inspected
                ]
        by_hash = {result.infohash: result for result in inspected}
        return [
            by_hash[entry.infohash] if isinstance(entry, _Magnet) else entry
            for entry in entries
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ensure_available(self) -> None:
        """Verify the isolated qBittorrent dependency before a batch starts."""
        await self._login()

    async def _login(self) -> None:
        async with self._login_lock:
            if self._logged_in:
                return
            try:
                response = await self._client.post(
                    "/api/v2/auth/login",
                    data={"username": self._username, "password": self._password},
                    timeout=self._request_timeout,
                )
            except httpx.HTTPError as exc:
                raise _ApiError("login_unavailable") from exc
            if response.status_code in {401, 403} or response.text.strip() == "Fails.":
                raise _ApiError("authentication_failed")
            if response.status_code >= 400:
                raise _ApiError("login_unavailable")
            if not (
                response.text.strip() == "Ok."
                or (response.status_code == 204 and not response.content)
            ):
                raise _ApiError("malformed_response")

            try:
                version_response = await self._client.get(
                    "/api/v2/app/version", timeout=self._request_timeout
                )
                version_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise _UnsupportedError("incompatible_qbittorrent") from exc
            version = _parse_version(version_response.text)
            if version is None or version < MINIMUM_QBITTORRENT_VERSION:
                raise _UnsupportedError("incompatible_qbittorrent")
            self._logged_in = True

    async def _inspect_one(
        self, item: _Magnet, batch_marker: str, marker_used: asyncio.Event
    ) -> QbittorrentInspectionResult:
        result: QbittorrentInspectionResult | None = None
        add_attempted = False
        cancelled = False
        async with self._hash_guard(item.infohash):
            try:
                existing = await self._torrent_info(hashes=item.infohash)
                if _matching_torrent(existing, item.infohash) is not None:
                    result = await self._inspect_existing(item.infohash)
                else:
                    add_attempted = True
                    marker_used.set()
                    await self._shielded_add(item.uri, batch_marker)
                    deadline = time.monotonic() + self._item_timeout
                    while result is None:
                        torrents = await self._torrent_info(hashes=item.infohash)
                        torrent = _matching_torrent(torrents, item.infohash)
                        if torrent is not None:
                            if not _has_tag(torrent, batch_marker):
                                result = QbittorrentInspectionResult(
                                    infohash=item.infohash,
                                    status=InspectionStatus.FAILED,
                                    error_code="ownership_conflict",
                                )
                                break
                            state = torrent.get("state")
                            if not isinstance(state, str):
                                raise _ApiError("malformed_response")
                            normalized_state = state.casefold()
                            if _has_metadata(torrent):
                                downloaded = torrent.get("downloaded")
                                if isinstance(downloaded, int) and downloaded > 0:
                                    result = QbittorrentInspectionResult(
                                        infohash=item.infohash,
                                        status=InspectionStatus.FAILED,
                                        error_code="metadata_stop_failed",
                                    )
                                else:
                                    files = await self._torrent_files(item.infohash)
                                    result = replace(
                                        _summarize(item.infohash, files),
                                        result_source="new_torrent",
                                    )
                            elif normalized_state in {
                                "checkingresumedata",
                                "metadl",
                                "queueddl",
                            }:
                                downloaded = torrent.get("downloaded")
                                if isinstance(downloaded, int) and downloaded > 0:
                                    result = QbittorrentInspectionResult(
                                        infohash=item.infohash,
                                        status=InspectionStatus.FAILED,
                                        error_code="metadata_stop_failed",
                                    )
                            elif normalized_state in {"pauseddl", "stoppeddl"}:
                                files = await self._torrent_files(item.infohash)
                                if not files:
                                    # 从未取得元数据时 qB 返回空文件列表:
                                    # 把空清单当 VERIFIED 证据会让内容检测误判为空资源。
                                    result = QbittorrentInspectionResult(
                                        infohash=item.infohash,
                                        status=InspectionStatus.FAILED,
                                        error_code="metadata_stop_failed",
                                        result_source="new_torrent",
                                    )
                                    break
                                result = replace(
                                    _summarize(item.infohash, files),
                                    result_source="new_torrent",
                                )
                                break
                            else:
                                result = QbittorrentInspectionResult(
                                    infohash=item.infohash,
                                    status=InspectionStatus.FAILED,
                                    error_code="metadata_stop_failed",
                                )
                                break
                        if time.monotonic() >= deadline:
                            result = QbittorrentInspectionResult(
                                infohash=item.infohash,
                                status=InspectionStatus.TIMEOUT,
                                error_code="metadata_timeout",
                            )
                            break
                        await asyncio.sleep(
                            min(
                                self._poll_interval,
                                max(0.0, deadline - time.monotonic()),
                            )
                        )
            except asyncio.CancelledError:
                cancelled = True
            except _UnsupportedError as exc:
                result = QbittorrentInspectionResult(
                    infohash=item.infohash,
                    status=InspectionStatus.UNSUPPORTED,
                    error_code=exc.code,
                )
            except _ApiError as exc:
                result = QbittorrentInspectionResult(
                    infohash=item.infohash,
                    status=InspectionStatus.FAILED,
                    error_code=exc.code,
                )
            finally:
                if add_attempted:
                    try:
                        await self._shielded_cleanup(item.infohash, batch_marker)
                    except _ApiError:
                        result = replace(
                            result
                            or QbittorrentInspectionResult(
                                infohash=item.infohash,
                                status=InspectionStatus.FAILED,
                            ),
                            status=InspectionStatus.FAILED,
                            error_code="cleanup_failed",
                        )
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    cancelled = True
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result or QbittorrentInspectionResult(
            infohash=item.infohash,
            status=InspectionStatus.FAILED,
            error_code="internal_error",
        )

    async def _inspect_existing(
        self, infohash: str
    ) -> QbittorrentInspectionResult:
        deadline = time.monotonic() + self._item_timeout
        while True:
            torrents = await self._torrent_info(hashes=infohash)
            torrent = _matching_torrent(torrents, infohash)
            if torrent is None:
                return QbittorrentInspectionResult(
                    infohash=infohash,
                    status=InspectionStatus.FAILED,
                    error_code="existing_torrent_unreadable",
                )
            state = torrent.get("state")
            normalized_state = state.casefold() if isinstance(state, str) else ""
            if _has_metadata(torrent):
                try:
                    files = await self._torrent_files(infohash)
                except _ApiError:
                    return QbittorrentInspectionResult(
                        infohash=infohash,
                        status=InspectionStatus.FAILED,
                        error_code="existing_torrent_unreadable",
                    )
                return replace(
                    _summarize(infohash, files), result_source="existing_torrent"
                )
            if normalized_state in {"metadl", "queuedl", "queueddl"}:
                if time.monotonic() >= deadline:
                    return QbittorrentInspectionResult(
                        infohash=infohash,
                        status=InspectionStatus.TIMEOUT,
                        error_code="metadata_timeout",
                        result_source="existing_torrent",
                    )
            else:
                try:
                    files = await self._torrent_files(infohash)
                except _ApiError:
                    return QbittorrentInspectionResult(
                        infohash=infohash,
                        status=InspectionStatus.FAILED,
                        error_code="existing_torrent_unreadable",
                    )
                if not files:
                    # 他人添加的既有 torrent 可能停在 stoppedDL/error/missingFiles
                    # 且从未取得元数据:qB 返回空文件列表。不得把空清单当已验证
                    # 证据(内容检测会误判为空资源),按元数据获取失败处理。
                    return QbittorrentInspectionResult(
                        infohash=infohash,
                        status=InspectionStatus.FAILED,
                        error_code="metadata_stop_failed",
                        result_source="existing_torrent",
                    )
                return replace(
                    _summarize(infohash, files), result_source="existing_torrent"
                )
            await asyncio.sleep(
                min(self._poll_interval, max(0.0, deadline - time.monotonic()))
            )

    async def _torrent_exists(self, infohash: str) -> bool:
        torrents = await self._torrent_info(hashes=infohash)
        return _matching_torrent(torrents, infohash) is not None

    @asynccontextmanager
    async def _hash_guard(self, infohash: str):
        async with self._hash_table_lock:
            state = self._hash_locks.get(infohash)
            if state is None:
                state = _HashLockState(lock=asyncio.Lock())
                self._hash_locks[infohash] = state
            state.users += 1
        try:
            async with state.lock:
                yield
        finally:
            await self._shielded_hash_release(infohash, state)

    async def _shielded_hash_release(
        self, infohash: str, state: _HashLockState
    ) -> None:
        release_task = asyncio.create_task(self._release_hash(infohash, state))
        while True:
            try:
                await asyncio.shield(release_task)
                return
            except asyncio.CancelledError:
                if release_task.done():
                    release_task.result()
                    return

    async def _release_hash(self, infohash: str, state: _HashLockState) -> None:
        async with self._hash_table_lock:
            state.users -= 1
            if state.users == 0 and self._hash_locks.get(infohash) is state:
                del self._hash_locks[infohash]

    async def _shielded_cleanup(self, infohash: str, marker: str) -> None:
        cleanup_task = asyncio.create_task(self._cleanup(infohash, marker))
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_task.result()
                    return

    async def _shielded_marker_cleanup(self, marker: str) -> None:
        cleanup_task = asyncio.create_task(self._cleanup_marker_with_semaphore(marker))
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_task.result()
                    return

    async def _cleanup_marker_with_semaphore(self, marker: str) -> None:
        async with self._semaphore:
            await self._cleanup_marker(marker)

    async def _shielded_add(self, magnet: str, marker: str) -> None:
        add_task = asyncio.create_task(self._add(magnet, marker))
        cancelled = False
        while not add_task.done():
            try:
                await asyncio.shield(add_task)
            except asyncio.CancelledError:
                cancelled = True
        add_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _add(self, magnet: str, marker: str) -> None:
        urls = _with_fallback_http_trackers(magnet)
        try:
            response = await self._client.post(
                "/api/v2/torrents/add",
                data={
                    "urls": urls,
                    "category": marker,
                    "tags": marker,
                    "stopCondition": "MetadataReceived",
                },
                timeout=self._request_timeout,
            )
        except httpx.HTTPError as exc:
            raise _ApiError("add_failed") from exc
        if response.status_code in {400, 404, 409} or response.text.strip() == "Fails.":
            raise _UnsupportedError("metadata_stop_unsupported")
        if response.status_code >= 400 or not _add_succeeded(response):
            raise _ApiError("add_failed")

    async def _torrent_info(self, **params: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(
                "/api/v2/torrents/info",
                params=params,
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _ApiError("api_unavailable") from exc
        except ValueError as exc:
            raise _ApiError("malformed_response") from exc
        if not isinstance(payload, list) or not all(
            isinstance(row, dict) and isinstance(row.get("hash"), str)
            for row in payload
        ):
            raise _ApiError("malformed_response")
        return payload

    async def _torrent_files(self, infohash: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(
                "/api/v2/torrents/files",
                params={"hash": infohash},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _ApiError("api_unavailable") from exc
        except ValueError as exc:
            raise _ApiError("malformed_response") from exc
        if not isinstance(payload, list) or not all(
            _valid_file(row) for row in payload
        ):
            raise _ApiError("malformed_response")
        return payload

    async def _cleanup(self, infohash: str, marker: str) -> None:
        torrents = await self._torrent_info(tag=marker)
        torrent = _matching_torrent(torrents, infohash)
        if torrent is None or not _has_tag(torrent, marker):
            return
        try:
            response = await self._client.post(
                "/api/v2/torrents/delete",
                data={"hashes": infohash, "deleteFiles": "true"},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _ApiError("cleanup_failed") from exc

    async def _cleanup_marker(self, marker: str) -> None:
        if await self._torrent_info(tag=marker):
            raise _ApiError("cleanup_failed")
        for path, data in (
            ("/api/v2/torrents/deleteTags", {"tags": marker}),
            ("/api/v2/torrents/removeCategories", {"categories": marker}),
        ):
            try:
                response = await self._client.post(
                    path,
                    data=data,
                    timeout=self._request_timeout,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise _ApiError("cleanup_failed") from exc


def _extract_infohash(uri: object) -> str | None:
    if not isinstance(uri, str):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.casefold() != "magnet":
        return None
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() != "xt" or not value.casefold().startswith("urn:btih:"):
            continue
        raw_hash = value[9:]
        if re.fullmatch(r"[0-9a-fA-F]{40}", raw_hash):
            return raw_hash.casefold()
        if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", raw_hash):
            try:
                return base64.b32decode(raw_hash.upper()).hex()
            except (binascii.Error, ValueError):
                return None
    return None


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    parts = match.groups()
    if any(len(part) > MAX_VERSION_COMPONENT_DIGITS for part in parts):
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def _with_fallback_http_trackers(magnet: str) -> str:
    """Append reachable public HTTP trackers to a magnet URI.

    Releases indexed by PanSou often carry only an infohash or UDP trackers,
    which cannot fetch metadata when UDP/DHT egress is restricted.  Adding a
    couple of HTTP trackers gives qBittorrent a working metadata path without
    touching the original trackers.
    """

    if not isinstance(magnet, str) or not magnet:
        return magnet
    if not magnet.casefold().startswith("magnet:"):
        return magnet
    appended = "".join(
        f"&tr={quote(tracker, safe='')}" for tracker in _FALLBACK_HTTP_TRACKERS
    )
    return magnet + appended


def _add_succeeded(response: httpx.Response) -> bool:
    body = response.text.strip()
    if body in {"", "Ok."}:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("success_count") == 1
        and payload.get("failure_count") == 0
        and payload.get("pending_count") == 0
        and isinstance(payload.get("added_torrent_ids"), list)
        and len(payload["added_torrent_ids"]) == 1
    )


def _matching_torrent(
    torrents: list[dict[str, Any]], infohash: str
) -> dict[str, Any] | None:
    return next(
        (
            torrent
            for torrent in torrents
            if torrent["hash"].casefold() == infohash.casefold()
        ),
        None,
    )


def _has_tag(torrent: dict[str, Any], marker: str) -> bool:
    tags = torrent.get("tags")
    return isinstance(tags, str) and marker in {
        tag.strip() for tag in tags.split(",") if tag.strip()
    }


def _has_metadata(torrent: dict[str, Any]) -> bool:
    """qB exposes this independently of its transient state string."""
    return torrent.get("has_metadata") is True


def _valid_file(row: object) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and isinstance(row.get("size"), int)
        and row["size"] >= 0
    )


def _summarize(
    infohash: str, files: list[dict[str, Any]]
) -> QbittorrentInspectionResult:
    videos: list[tuple[str, int, bool]] = []
    subtitle_count = 0
    suspicious_count = 0
    total_size = 0
    for item in files:
        name = item["name"]
        size = item["size"]
        basename = PurePosixPath(name.replace("\\", "/")).name
        extension = PurePosixPath(basename).suffix.casefold()
        suspicious = bool(SUSPICIOUS_NAME.search(name.replace("\\", "/"))) or (
            extension in SUSPICIOUS_EXTENSIONS
        )
        total_size += size
        if extension in VIDEO_EXTENSIONS:
            videos.append((basename, size, suspicious))
        if extension in SUBTITLE_EXTENSIONS:
            subtitle_count += 1
        if suspicious:
            suspicious_count += 1

    feature_videos = [video for video in videos if not video[2]] or videos
    largest = max(feature_videos, key=lambda video: video[1], default=None)
    video_size = sum(video[1] for video in videos)
    summary = (
        f"{len(videos)} video(s), {subtitle_count} subtitle(s), "
        f"{suspicious_count} suspicious file(s)"
    )
    return QbittorrentInspectionResult(
        infohash=infohash,
        status=InspectionStatus.VERIFIED,
        total_size_bytes=total_size,
        file_count=len(files),
        video_file_count=len(videos),
        video_size_bytes=video_size,
        subtitle_count=subtitle_count,
        sample_count=suspicious_count,
        largest_video_name=largest[0] if largest else None,
        content_summary=summary,
    )
