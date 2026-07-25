"""Persisted logging settings and redacted file-backed application logs."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    return _ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)


class LogStore:
    def __init__(self, state_directory: Path) -> None:
        self._directory = state_directory
        self._path = state_directory / "watch-assistant.log"
        self._lock = asyncio.Lock()

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
            self._append_sync(
                level=level,
                category=category,
                message=message,
                retention_days=retention_days,
                max_file_mb=max_file_mb,
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
            records = self._read_sync(category=category)
        if cursor is not None:
            records = [record for record in records if record["id"] > cursor]
        next_cursor = records[limit - 1]["id"] if len(records) > limit else None
        return records[:limit], next_cursor

    def _append_sync(
        self,
        *,
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
        record = {
            "id": self._next_id_sync(),
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

    def _next_id_sync(self) -> int:
        records = self._read_sync(category=None)
        return max((record["id"] for record in records), default=0) + 1

    def _read_sync(self, *, category: LogCategory | None) -> list[dict[str, Any]]:
        if not self._directory.exists():
            return []
        paths = sorted(
            (
                path
                for path in self._directory.glob("watch-assistant.log*")
                if path.is_file()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        records: list[dict[str, Any]] = []
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    if not isinstance(record.get("id"), int) or record["id"] < 1:
                        continue
                    if not isinstance(record.get("timestamp"), str):
                        continue
                    if not isinstance(record.get("message"), str):
                        continue
                    if record.get("category") not in {
                        item.value for item in LogCategory
                    }:
                        continue
                    if record.get("level") not in {item.value for item in LoggingLevel}:
                        continue
                    record["message"] = redact_log_message(record["message"])
                    if category is None or record["category"] == category.value:
                        records.append(record)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return sorted(records, key=lambda record: record["id"])

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
        async with self._session_factory() as session:
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
        await self.log_store.append(
            level=LoggingLevel.INFO,
            category=LogCategory.SECURITY,
            message="logging settings updated",
            retention_days=response.retention_days,
            max_file_mb=response.max_file_mb,
        )
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
