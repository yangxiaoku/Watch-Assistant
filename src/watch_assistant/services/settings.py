"""Persisted logging settings and redacted file-backed application logs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import weakref
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import ApplicationSettings
from watch_assistant.schemas import (
    ContentPolicyPatch,
    ContentPolicyResponse,
    InspectionSettingsPatch,
    InspectionSettingsResponse,
    LogCategory,
    LoggingLevel,
    LoggingSettingsPatch,
    LoggingSettingsResponse,
)
from watch_assistant.services.content_policy import (
    ContentPolicy,
    content_policy_from_json,
    normalize_keywords,
)

DEFAULT_LEVEL = LoggingLevel.INFO
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_FILE_MB = 10
SETTINGS_ID = "default"
MAX_MESSAGE_CHARS = 4096
MESSAGE_TRUNCATION_MARKER = "[TRUNCATED]"

logger = logging.getLogger(__name__)

_MUTATION_LOCKS: weakref.WeakKeyDictionary[object, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def shared_settings_mutation_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> asyncio.Lock:
    """Return the process-local lock shared by every settings writer."""
    lock = _MUTATION_LOCKS.get(session_factory)
    if lock is None:
        lock = asyncio.Lock()
        _MUTATION_LOCKS[session_factory] = lock
    return lock


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
    r"(?:\b(?:share[_ -]?code|extract[_ -]?code|receive[_ -]?code|"
    r"access[_ -]?code)\b|\u63d0\u53d6\u7801)"
    r"\s*[:=\uFF1A]?\s*[A-Za-z0-9_-]+",
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
_ROTATED_LOG = re.compile(r"^watch-assistant\.log\.(\d{14})-(\d+)$")


class SettingsConflict(ValueError):
    pass


class ContentPolicyValidationError(ValueError):
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
        latest_id = 0
        for path in self._log_paths_sync():
            for line in self._iter_lines_reverse_sync(path):
                if line is None:
                    continue
                record = self._parse_record(line)
                if record is not None and record["id"] > latest_id:
                    latest_id = record["id"]
        return latest_id

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
        current: Path | None = None
        rotated: list[tuple[str, int, Path]] = []
        try:
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    if path.name == self._path.name:
                        current = path
                        continue
                    match = _ROTATED_LOG.fullmatch(path.name)
                    if match is None:
                        continue
                    rotated.append((match.group(1), int(match.group(2)), path))
                except (OSError, ValueError):
                    continue
        except OSError:
            return []
        rotated.sort(key=lambda item: (item[0], item[1]), reverse=True)
        paths = [current] if current is not None else []
        paths.extend(path for _timestamp, _record_id, path in rotated)
        return paths

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
        self._settings_lock = shared_settings_mutation_lock(session_factory)
        self._write_windows: dict[str, deque[datetime]] = {}

    async def get_logging(self) -> LoggingSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _settings_response(settings)

    def check_write_rate_limit(
        self,
        identity: str,
        *,
        now: datetime | None = None,
        limit: int = 12,
        window: timedelta = timedelta(minutes=1),
    ) -> None:
        checked_at = now or datetime.now(UTC)
        bucket = self._write_windows.setdefault(identity, deque())
        cutoff = checked_at - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise SettingsConflict("rate_limited")
        bucket.append(checked_at)

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
                message="settings.changed status=logging",
                retention_days=response.retention_days,
                max_file_mb=response.max_file_mb,
            )
        except Exception:  # noqa: BLE001 - audit failure cannot break settings
            logger.warning("settings audit log write failed")
        return response

    async def get_content_policy(self) -> ContentPolicyResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _content_policy_response(_content_policy(settings))

    async def update_content_policy(
        self, patch: ContentPolicyPatch
    ) -> ContentPolicyResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != patch.revision:
                raise SettingsConflict
            current = _content_policy(settings)
            values = patch.model_dump(exclude_none=True)
            try:
                keywords = normalize_keywords(
                    values.get("blocked_keywords", current.blocked_keywords)
                )
            except ValueError as exc:
                raise ContentPolicyValidationError(str(exc)) from None
            updated = ContentPolicy(
                hide_adult_media=values.get(
                    "hide_adult_media", current.hide_adult_media
                ),
                hide_suspicious_resources=values.get(
                    "hide_suspicious_resources", current.hide_suspicious_resources
                ),
                hide_low_quality_resources=values.get(
                    "hide_low_quality_resources", current.hide_low_quality_resources
                ),
                blocked_keywords=keywords,
                revision=settings.revision + 1,
            )
            settings.content_policy_json = json.dumps(
                {
                    "hide_adult_media": updated.hide_adult_media,
                    "hide_suspicious_resources": updated.hide_suspicious_resources,
                    "hide_low_quality_resources": updated.hide_low_quality_resources,
                    "blocked_keywords": list(updated.blocked_keywords),
                },
                ensure_ascii=False,
            )
            settings.revision = updated.revision
            await session.commit()
            response = _content_policy_response(updated)
        await self.log_event(
            "settings.changed",
            level=LoggingLevel.INFO,
            fields={"status": "content_policy"},
        )
        return response

    async def log_event(
        self,
        event: str,
        *,
        level: LoggingLevel = LoggingLevel.INFO,
        fields: dict[str, object] | None = None,
    ) -> None:
        category = _EVENT_CATEGORIES.get(event)
        if category is None:
            return
        try:
            async with self._settings_lock, self._session_factory() as session:
                settings = await self._get_or_create(session)
                configured_level = LoggingLevel(settings.logging_level)
                retention_days = settings.retention_days
                max_file_mb = settings.max_file_mb
            if _level_rank(level) < _level_rank(configured_level):
                return
            await self.log_store.append(
                level=level,
                category=category,
                message=_event_message(event, fields),
                retention_days=retention_days,
                max_file_mb=max_file_mb,
            )
        except Exception:  # noqa: BLE001 - observability cannot break requests
            logger.warning("business log write failed")

    async def get_inspection(self) -> InspectionSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _inspection_response(settings)

    async def update_inspection(
        self, patch: InspectionSettingsPatch
    ) -> InspectionSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != patch.revision:
                raise SettingsConflict
            settings.inspection_auto_start_enabled = patch.auto_start_enabled
            settings.revision += 1
            await session.commit()
            response = _inspection_response(settings)
        await self.log_event(
            "settings.changed",
            level=LoggingLevel.INFO,
            fields={"status": "inspection"},
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
                inspection_auto_start_enabled=True,
                revision=0,
                content_policy_json="{}",
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


_EVENT_CATEGORIES = {
    "application.startup": LogCategory.SYSTEM,
    "application.readiness": LogCategory.SYSTEM,
    "warmup.started": LogCategory.CACHE,
    "warmup.completed": LogCategory.CACHE,
    "warmup.failed": LogCategory.CACHE,
    "search.started": LogCategory.SEARCH,
    "search.completed": LogCategory.SEARCH,
    "search.failed": LogCategory.SEARCH,
    "search.cache_hit": LogCategory.CACHE,
    "resources.page_served": LogCategory.CACHE,
    "inspection.batch_started": LogCategory.INSPECTION,
    "inspection.batch_completed": LogCategory.INSPECTION,
    "inspection.batch_failed": LogCategory.INSPECTION,
    "p115.readiness": LogCategory.P115,
    "task.submitted": LogCategory.SYSTEM,
    "task.accepted": LogCategory.SYSTEM,
    "task.failed": LogCategory.SYSTEM,
    "settings.changed": LogCategory.SECURITY,
}
_EVENT_FIELDS = frozenset(
    {
        "status",
        "count",
        "total",
        "duration_ms",
        "page",
        "media_type",
        "season",
        "hidden_count",
        "hidden_suspicious",
        "hidden_low_quality",
        "hidden_keyword",
    }
)
_LEVEL_RANK = {
    LoggingLevel.DEBUG: 10,
    LoggingLevel.INFO: 20,
    LoggingLevel.WARNING: 30,
    LoggingLevel.ERROR: 40,
}


def _level_rank(level: LoggingLevel) -> int:
    return _LEVEL_RANK[LoggingLevel(level)]


def _event_message(event: str, fields: dict[str, object] | None) -> str:
    safe: list[str] = []
    for key, value in sorted((fields or {}).items()):
        if key not in _EVENT_FIELDS:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float, str)):
            rendered = str(value)
        else:
            continue
        safe.append(f"{key}={rendered[:80]}")
    return " ".join([event, *safe])


def _content_policy(settings: ApplicationSettings) -> ContentPolicy:
    return content_policy_from_json(settings.content_policy_json, settings.revision)


def _content_policy_response(policy: ContentPolicy) -> ContentPolicyResponse:
    return ContentPolicyResponse(
        hide_adult_media=policy.hide_adult_media,
        hide_suspicious_resources=policy.hide_suspicious_resources,
        hide_low_quality_resources=policy.hide_low_quality_resources,
        blocked_keywords=list(policy.blocked_keywords),
        revision=policy.revision,
    )


def _inspection_response(settings: ApplicationSettings) -> InspectionSettingsResponse:
    return InspectionSettingsResponse(
        auto_start_enabled=bool(settings.inspection_auto_start_enabled),
        revision=settings.revision,
    )
