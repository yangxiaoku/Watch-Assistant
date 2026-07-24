"""Isolated qBittorrent adapter for magnet metadata inspection."""

import asyncio
import base64
import binascii
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

VIDEO_EXTENSIONS = {".avi", ".m2ts", ".mkv", ".mov", ".mp4", ".ts"}
SUBTITLE_EXTENSIONS = {".ass", ".srt", ".ssa", ".sub", ".vtt"}
SUSPICIOUS_EXTENSIONS = {".bat", ".cmd", ".com", ".exe", ".msi", ".scr"}
SUSPICIOUS_NAME = re.compile(
    r"(?:^|[/._\-\s])(sample|trailer|proof)(?:[/._\-\s]|$)", re.IGNORECASE
)


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


@dataclass(frozen=True)
class _Magnet:
    uri: str
    infohash: str


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
        item_timeout: float = 60.0,
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
        semaphore = asyncio.Semaphore(self._concurrency)

        async def run(item: _Magnet) -> QbittorrentInspectionResult:
            async with semaphore:
                return await self._inspect_one(item, batch_marker)

        inspected = await asyncio.gather(*(run(item) for item in parsed))
        by_hash = {result.infohash: result for result in inspected}
        return [
            by_hash[entry.infohash] if isinstance(entry, _Magnet) else entry
            for entry in entries
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _login(self) -> None:
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
        if response.text.strip() != "Ok.":
            raise _ApiError("malformed_response")
        self._logged_in = True

    async def _inspect_one(
        self, item: _Magnet, batch_marker: str
    ) -> QbittorrentInspectionResult:
        result: QbittorrentInspectionResult | None = None
        add_attempted = False
        try:
            if await self._torrent_exists(item.infohash):
                result = QbittorrentInspectionResult(
                    infohash=item.infohash,
                    status=InspectionStatus.UNSUPPORTED,
                    error_code="existing_torrent",
                )
            else:
                add_attempted = True
                await self._add(item.uri, batch_marker)
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
                        if state.casefold() != "metadl":
                            files = await self._torrent_files(item.infohash)
                            result = _summarize(item.infohash, files)
                            break
                    if time.monotonic() >= deadline:
                        result = QbittorrentInspectionResult(
                            infohash=item.infohash,
                            status=InspectionStatus.TIMEOUT,
                            error_code="metadata_timeout",
                        )
                        break
                    await asyncio.sleep(
                        min(self._poll_interval, max(0.0, deadline - time.monotonic()))
                    )
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
        if add_attempted:
            try:
                await self._cleanup(item.infohash, batch_marker)
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
        return result or QbittorrentInspectionResult(
            infohash=item.infohash,
            status=InspectionStatus.FAILED,
            error_code="internal_error",
        )

    async def _torrent_exists(self, infohash: str) -> bool:
        torrents = await self._torrent_info(hashes=infohash)
        return _matching_torrent(torrents, infohash) is not None

    async def _add(self, magnet: str, marker: str) -> None:
        try:
            response = await self._client.post(
                "/api/v2/torrents/add",
                data={
                    "urls": magnet,
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
        if response.status_code >= 400 or response.text.strip() != "Ok.":
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
