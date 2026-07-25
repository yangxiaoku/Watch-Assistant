"""Persisted logging settings and redacted file-backed application logs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import ApplicationSettings
from watch_assistant.schemas import (
    LogCategory,
    LoggingLevel,
    LoggingSettingsPatch,
    LoggingSettingsResponse,
)

DEFAULT_LEVEL = LoggingLevel.INFO
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_FILE_MB = 10
SETTINGS_ID = "default"
MAX_MESSAGE_CHARS = 4096
MESSAGE_TRUNCATION_MARKER = "[TRUNCATED]"

logger = logging.getLogger(__name__)

_COOKIE_FIELD = re.compile(r"\b(?:UID|CID|KID|SEID)=[^;\s]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:cookie|token|password|passwd|secret|cid|infohash|hash)\b"
    r"\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"\b(?:cookie|token|password|passwd|secret)\s+[^\s,;]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_MAGNET = re.compile(r"magnet:\?[^\s]+", re.IGNORECASE)
_SHARE_URL = re.compile(
    r"https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/(?:s|share)/[^\s\"'<>]+",
    re.IGNORECASE,
)
_SHARE_CODE = re.compile(
    r"(?:\b(?:share[_ -]?code|extract[_ -]?code)\b|[\u63d0\u53d6\u7801])"
    r"\s*[:=]?\s*[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_TRACKER = re.compile(
    r"(?:udp|https?)://[^\s\"'<>]+/announce(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_INFOHASH = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
_URL_QUERY = re.compile(r"([?&][A-Za-z0-9_.-]+=)[^&#\s]+")
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|/)(?:[^\s\"']+[\\/])*[^\s\"']+"
)


class SettingsConflict(ValueError):
    pass


def redact_log_message(message: str) -> str:
    """Apply a second, pattern-based redaction before a message is persisted."""
    redacted = str(message)
    redacted = _SHARE_URL.sub("[REDACTED_SHARE]", redacted)
    redacted = _SHARE_CODE.sub("[REDACTED_SHARE]", redacted)
    redacted = _TRACKER.sub("[REDACTED_TRACKER]", redacted)
    redacted = _MAGNET.sub("[REDACTED_MAGNET]", redacted)
    redacted = _COOKIE_FIELD.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]", redacted
    )
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + "=[REDACTED]",
        redacted,
    )
    redacted = _SECRET_VALUE.sub(
        lambda match: match.group(0).split(None, 1)[0] + " [REDACTED]",
        redacted,
    )
    redacted = _URL_QUERY.sub(r"\1[REDACTED]", redacted)
    redacted = _INFOHASH.sub("[REDACTED_INFOHASH]", redacted)
    redacted = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)
    if len(redacted) > MAX_MESSAGE_CHARS:
        redacted = (
            redacted[: MAX_MESSAGE_CHARS - len(MESSAGE_TRUNCATION_MARKER)]
            + MESSAGE_TRUNCATION_MARKER
        )
    return redacted


class LogStore:
    def __init__(self, state_directory: Path) -> None:
        self._directory = state_directory
        self._path = state_directory / "watch-assistant.log"
        self._lock = asyncio.Lock()
        self._next_id: int | None = None

    async def append(
        self,
        *,
        level: LoggingLevel,
        category: LogCategory,
        message: str,
        retention_days: int,
        max_file_mb: int,
    ) -> None:
        level = LoggingLevel(level)
        category = LogCategory(category)
        async with self._lock:
            await asyncio.to_thread(
                self._append_sync,
                level,
                category,
                message,
                retention_days,
                max_file_mb,
            )

    async def list(
        self,
        *,
        cursor: int | None,
        limit: int,
        category: LogCategory | None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        if category is not None:
            category = LogCategory(category)
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sync,
                cursor,
                limit,
                category,
            )

    def _append_sync(
        self,
        level: LoggingLevel,
        category: LogCategory,
        message: str,
        retention_days: int,
        max_file_mb: int,
    ) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._directory, 0o700)
        except OSError:
            pass
        if self._next_id is None:
            self._next_id = self._latest_id_sync() + 1
        record_id = self._next_id
        self._next_id += 1
        record = {
            "id": record_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.value,
            "category": category.value,
            "message": redact_log_message(message),
        }
        line = (
            json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if (
            self._path.exists()
            and self._path.stat().st_size + len(line) > max_file_mb * 1024 * 1024
        ):
            rotated = self._directory / (
                f"watch-assistant.log.{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{record['id']}"
            )
            os.replace(self._path, rotated)
            self._chmod_file(rotated)
        with self._path.open("ab") as log_file:
            log_file.write(line)
        self._chmod_file(self._path)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        for path in self._directory.glob("watch-assistant.log.*"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _list_sync(
        self,
        cursor: int | None,
        limit: int,
        category: LogCategory | None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        records: list[dict[str, Any]] = []
        for path in self._log_paths_sync():
            for line in self._iter_lines_reverse_sync(path):
                if line is None:
                    continue
                record = self._parse_record(line)
                if record is None:
                    continue
                if cursor is not None and record["id"] >= cursor:
                    continue
                if category is not None and record["category"] != category.value:
                    continue
                records.append(record)
                if len(records) >= limit + 1:
                    next_cursor = records[limit - 1]["id"]
                    return records[:limit], next_cursor
        return records, None

    def _latest_id_sync(self) -> int:
        for path in self._log_paths_sync():
            for line in self._iter_lines_reverse_sync(path):
                if line is None:
                    continue
                record = self._parse_record(line)
                if record is not None:
                    return record["id"]
        return 0

    @staticmethod
    def _iter_lines_reverse_sync(path: Path) -> Iterator[str | None]:
        chunk_size = 64 * 1024
        try:
            with path.open("rb") as log_file:
                position = log_file.seek(0, os.SEEK_END)
                remainder = b""
                while position > 0:
                    read_size = min(chunk_size, position)
                    position -= read_size
                    log_file.seek(position)
                    data = log_file.read(read_size) + remainder
                    parts = data.split(b"\n")
                    remainder = parts[0]
                    for raw_line in reversed(parts[1:]):
                        try:
                            yield raw_line.decode("utf-8")
                        except UnicodeDecodeError:
                            yield None
                if remainder:
                    try:
                        yield remainder.decode("utf-8")
                    except UnicodeDecodeError:
                        yield None
        except OSError:
            return

    def _log_paths_sync(self) -> list[Path]:
        try:
            if not self._directory.exists():
                return []
            candidates = self._directory.glob("watch-assistant.log*")
        except OSError:
            return []
        paths: list[tuple[int, str, Path]] = []
        try:
            for path in candidates:
                try:
                    if path.is_file():
                        paths.append((path.stat().st_mtime_ns, path.name, path))
                except OSError:
                    continue
        except OSError:
            return []
        paths.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [path for _mtime, _name, path in paths]

    @staticmethod
    def _parse_record(line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            record_id = record.get("id")
            if (
                not isinstance(record_id, int)
                or isinstance(record_id, bool)
                or record_id < 1
            ):
                return None
            timestamp = record.get("timestamp")
            if not isinstance(timestamp, str):
                return None
            parsed_timestamp = datetime.fromisoformat(timestamp)
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                return None
            level = LoggingLevel(record.get("level"))
            category = LogCategory(record.get("category"))
            message = record.get("message")
            if not isinstance(message, str):
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return {
            "id": record_id,
            "timestamp": timestamp,
            "level": level.value,
            "category": category.value,
            "message": redact_log_message(message),
        }

    @staticmethod
    def _chmod_file(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


class SettingsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        state_directory: Path,
    ) -> None:
        self._session_factory = session_factory
        self.log_store = LogStore(state_directory)
        self._settings_lock = asyncio.Lock()

    async def get_logging(self) -> LoggingSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _settings_response(settings)

    async def update_logging(
        self, patch: LoggingSettingsPatch
    ) -> LoggingSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != patch.revision:
                raise SettingsConflict
            values = patch.model_dump(exclude_none=True)
            values.pop("revision", None)
            for key, value in values.items():
                setattr(settings, f"logging_{key}" if key == "level" else key, value)
            settings.revision += 1
            await session.commit()
            response = _settings_response(settings)
        try:
            await self.log_store.append(
                level=LoggingLevel.INFO,
                category=LogCategory.SECURITY,
                message="logging settings updated",
                retention_days=response.retention_days,
                max_file_mb=response.max_file_mb,
            )
        except OSError:
            logger.warning("settings audit log write failed")
        return response

    async def _get_or_create(self, session: AsyncSession) -> ApplicationSettings:
        settings = await session.get(ApplicationSettings, SETTINGS_ID)
        if settings is None:
            settings = ApplicationSettings(
                id=SETTINGS_ID,
                logging_level=DEFAULT_LEVEL.value,
                retention_days=DEFAULT_RETENTION_DAYS,
                max_file_mb=DEFAULT_MAX_FILE_MB,
                revision=0,
            )
            session.add(settings)
            await session.commit()
        return settings


def _settings_response(settings: ApplicationSettings) -> LoggingSettingsResponse:
    return LoggingSettingsResponse(
        level=LoggingLevel(settings.logging_level),
        retention_days=settings.retention_days,
        max_file_mb=settings.max_file_mb,
        revision=settings.revision,
    )
