"""Persisted logging settings and redacted file-backed application logs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import ApplicationSettings, AuditRecord, utc_now
from watch_assistant.schemas import (
    ContentPolicyPatch,
    ContentPolicyResponse,
    InspectionSettingsPatch,
    InspectionSettingsResponse,
    LogCategory,
    LoggingLevel,
    LoggingSettingsPatch,
    LoggingSettingsResponse,
    OrganizationSettingsPatch,
    OrganizationSettingsResponse,
)
from watch_assistant.services.content_policy import (
    ContentPolicy,
    content_policy_from_json,
    normalize_keywords,
)
from watch_assistant.services.event_catalog import (
    get_event_definition,
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


class OrganizationSettingsValidationError(ValueError):
    pass


_CID_PATTERN = re.compile(r"^[1-9][0-9]{0,127}$")
_EXTENSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+_-]{0,15}$")
_DIRECTORY_LABEL_SEGMENT = re.compile(r"^[^\\/:*?\"<>|\r\n]+$")
DEFAULT_ORGANIZATION_VIDEO_EXTENSIONS = (
    "mkv",
    "mp4",
    "avi",
    "mov",
    "ts",
    "m2ts",
    "wmv",
    "flv",
    "webm",
)

_ORGANIZATION_DEFAULTS: dict[str, object] = {
    "schedule_enabled": False,
    # 自动整理默认开启：高置信度影片扫描后自动确认并归档，识别不确定的保留待人工确认。
    "auto_execute_enabled": True,
    "scan_interval_minutes": 30,
    "source_directory_ids": [],
    "source_directory_labels": [],
    "target_directory_id": None,
    "target_directory_label": None,
    "push_directory_id": None,
    "push_directory_label": None,
    "video_extensions": list(DEFAULT_ORGANIZATION_VIDEO_EXTENSIONS),
    "metadata_extensions": ["srt", "ass", "ssa", "sub", "vtt", "nfo", "jpg", "jpeg", "png", "webp"],
    "rename_enabled": True,
    "media_probe_enabled": True,
    "ai_identification_enabled": False,
    # 低于该阈值的未识别(需要人工确认)小文件在整理后自动删除到 115 回收站。
    "small_file_threshold_mb": 100.0,
    "cleanup_empty_directories": False,
    "strm_linkage_enabled": False,
    "operation_delay_seconds": 1.5,
    "include_children_category": False,
    "include_concert_category": False,
    "region_grouping_enabled": True,
    "year_grouping_enabled": False,
    "prefer_remux": True,
    "prefer_resolution": True,
    "prefer_dolby": False,
    "conflict_mode": 2,
    "multi_version_enabled": False,
}


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
        event_code: str = "legacy.log",
        event_version: int = 1,
        title_zh: str = "应用日志",
        message_zh: str | None = None,
        suggestion_zh: str | None = None,
        status: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        task_id: str | None = None,
        duration_ms: int | None = None,
        counts: dict[str, int] | None = None,
        error_code: str | None = None,
        context: dict[str, Any] | None = None,
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
                event_code,
                event_version,
                title_zh,
                message_zh if message_zh is not None else message,
                suggestion_zh,
                status,
                request_id,
                correlation_id,
                actor_type,
                actor_id,
                resource_type,
                resource_id,
                task_id,
                duration_ms,
                counts or {},
                error_code,
                context or {},
            )

    async def list(
        self,
        *,
        cursor: int | None,
        limit: int,
        category: LogCategory | None,
        level: LoggingLevel | None = None,
        event_code: str | None = None,
        status: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        task_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        if category is not None:
            category = LogCategory(category)
        start_time = _as_utc(start_time)
        end_time = _as_utc(end_time)
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sync,
                cursor,
                limit,
                category,
                level,
                event_code,
                status,
                request_id,
                correlation_id,
                task_id,
                actor_type,
                actor_id,
                resource_type,
                resource_id,
                start_time,
                end_time,
            )

    def _append_sync(
        self,
        level: LoggingLevel,
        category: LogCategory,
        message: str,
        retention_days: int,
        max_file_mb: int,
        event_code: str,
        event_version: int,
        title_zh: str,
        message_zh: str,
        suggestion_zh: str | None,
        status: str | None,
        request_id: str | None,
        correlation_id: str | None,
        actor_type: str | None,
        actor_id: str | None,
        resource_type: str | None,
        resource_id: str | None,
        task_id: str | None,
        duration_ms: int | None,
        counts: dict[str, int],
        error_code: str | None,
        context: dict[str, Any],
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
            "event_code": event_code,
            "event_version": event_version,
            "title_zh": redact_log_message(title_zh),
            "message_zh": redact_log_message(message_zh),
            "suggestion_zh": redact_log_message(suggestion_zh) if suggestion_zh else None,
            "status": _safe_scalar(status),
            "request_id": _safe_scalar(request_id),
            "correlation_id": _safe_scalar(correlation_id),
            "actor_type": _safe_scalar(actor_type),
            "actor_id": _safe_scalar(actor_id),
            "resource_type": _safe_scalar(resource_type),
            "resource_id": _safe_scalar(resource_id),
            "task_id": _safe_scalar(task_id),
            "duration_ms": duration_ms,
            "counts": _safe_counts(counts),
            "error_code": _safe_scalar(error_code),
            "context": _safe_context(context),
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
        level: LoggingLevel | None,
        event_code: str | None,
        status: str | None,
        request_id: str | None,
        correlation_id: str | None,
        task_id: str | None,
        actor_type: str | None,
        actor_id: str | None,
        resource_type: str | None,
        resource_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
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
                if level is not None and record["level"] != level.value:
                    continue
                if event_code is not None and record["event_code"] != event_code:
                    continue
                if status is not None and record.get("status") != status:
                    continue
                if request_id is not None and record.get("request_id") != request_id:
                    continue
                if correlation_id is not None and record.get("correlation_id") != correlation_id:
                    continue
                if task_id is not None and record.get("task_id") != task_id:
                    continue
                if actor_type is not None and record.get("actor_type") != actor_type:
                    continue
                if actor_id is not None and record.get("actor_id") != actor_id:
                    continue
                if resource_type is not None and record.get("resource_type") != resource_type:
                    continue
                if resource_id is not None and record.get("resource_id") != resource_id:
                    continue
                timestamp = datetime.fromisoformat(record["timestamp"])
                if start_time is not None and timestamp < start_time:
                    continue
                if end_time is not None and timestamp > end_time:
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
            "event_code": str(record.get("event_code") or "legacy.log"),
            "event_version": _positive_int(record.get("event_version"), 1),
            "title_zh": _safe_text(record.get("title_zh"), "应用日志"),
            "message_zh": redact_log_message(
                _safe_text(record.get("message_zh"), message)
            ),
            "suggestion_zh": _safe_optional_text(record.get("suggestion_zh")),
            "status": _safe_optional_text(record.get("status")),
            "request_id": _safe_optional_text(record.get("request_id")),
            "correlation_id": _safe_optional_text(record.get("correlation_id")),
            "actor_type": _safe_optional_text(record.get("actor_type")),
            "actor_id": _safe_optional_text(record.get("actor_id")),
            "resource_type": _safe_optional_text(record.get("resource_type")),
            "resource_id": _safe_optional_text(record.get("resource_id")),
            "task_id": _safe_optional_text(record.get("task_id")),
            "duration_ms": _nonnegative_int(record.get("duration_ms")),
            "counts": _safe_counts(record.get("counts")),
            "error_code": _safe_optional_text(record.get("error_code")),
            "context": _safe_context(record.get("context")),
        }

    @staticmethod
    def _chmod_file(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _safe_text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else default


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_scalar(value: object) -> str | None:
    if isinstance(value, str):
        return redact_log_message(value)[:256]
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _safe_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:64]: item
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, int)
        and not isinstance(item, bool)
        and 0 <= item <= 10_000_000
    }


def _safe_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:64]: _safe_context_item(item)
        for key, item in value.items()
        if isinstance(key, str) and _safe_context_item(item) is not None
    }


def _safe_context_item(value: object) -> object | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return redact_log_message(value)[:256]
    if isinstance(value, list):
        return [_safe_context_item(item) for item in value[:20]]
    return None


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
        self._event_sinks: list[Callable[..., Awaitable[object]]] = []

    def bind_event_sink(self, event_sink: Callable[..., Awaitable[object]]) -> None:
        """Attach an optional durable event consumer without changing callers."""
        if event_sink not in self._event_sinks:
            self._event_sinks.append(event_sink)

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
        self,
        patch: LoggingSettingsPatch,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
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
            self._add_audit_record(
                session,
                event="settings.changed",
                fields={"status": "logging", "changed_fields": sorted(values)},
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
            )
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
        self,
        patch: ContentPolicyPatch,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
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
            self._add_audit_record(
                session,
                event="settings.changed",
                fields={
                    "status": "content_policy",
                    "changed_fields": sorted(values),
                },
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
            )
            await session.commit()
            response = _content_policy_response(updated)
        await self.log_event(
            "settings.changed",
            level=LoggingLevel.INFO,
            fields={"status": "content_policy"},
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=request_id,
        )
        return response

    async def log_event(
        self,
        event: str,
        *,
        level: LoggingLevel = LoggingLevel.INFO,
        fields: dict[str, object] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        task_id: str | None = None,
        duration_ms: int | None = None,
        counts: dict[str, int] | None = None,
        error_code: str | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        definition = get_event_definition(event)
        if definition is None:
            safe_event = _safe_scalar(event) or "unknown"
            logger.error("unknown business event: %s", safe_event)
            unknown = get_event_definition("observability.unknown_event")
            if unknown is not None:
                try:
                    async with self._settings_lock, self._session_factory() as session:
                        settings = await self._get_or_create(session)
                        configured_level = LoggingLevel(settings.logging_level)
                        retention_days = settings.retention_days
                        max_file_mb = settings.max_file_mb
                    if _level_rank(LoggingLevel.ERROR) >= _level_rank(configured_level):
                        await self.log_store.append(
                            level=LoggingLevel.ERROR,
                            category=unknown.category,
                            message=_legacy_event_message(
                                unknown.code,
                                {"status": "unregistered", "event_code": safe_event},
                            ),
                            retention_days=retention_days,
                            max_file_mb=max_file_mb,
                            event_code=unknown.code,
                            event_version=unknown.version,
                            title_zh=unknown.title_zh,
                            message_zh=unknown.render(
                                {"status": "unregistered", "event_code": safe_event}
                            ),
                            suggestion_zh=unknown.suggestion_zh,
                            status="unregistered",
                            request_id=request_id,
                            correlation_id=correlation_id,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            resource_type=resource_type,
                            resource_id=resource_id,
                            task_id=task_id,
                            error_code="unknown_event",
                            context={"event_code": safe_event},
                        )
                except Exception:  # noqa: BLE001 - observability cannot break requests
                    logger.warning("unknown event audit write failed")
            return
        raw_fields = dict(fields or {})
        unknown_fields = set(raw_fields) - definition.allowed_fields
        if unknown_fields:
            logger.error(
                "unknown fields for business event %s: %s",
                event,
                ",".join(sorted(unknown_fields)),
            )
        safe_fields = {
            key: value
            for key, value in raw_fields.items()
            if key in definition.allowed_fields
        }
        if counts is None:
            counts = {
                key: value
                for key, value in safe_fields.items()
                if key in {"count", "total", "hidden_count", "hidden_suspicious", "hidden_low_quality", "hidden_keyword"}
                and isinstance(value, int)
                and not isinstance(value, bool)
            }
        status = _safe_optional_text(safe_fields.get("status"))
        error_code = error_code or _safe_optional_text(safe_fields.get("error_code"))
        duration_ms = duration_ms if duration_ms is not None else _nonnegative_int(safe_fields.get("duration_ms"))
        context = context or {
            key: value
            for key, value in safe_fields.items()
            if key not in {"status", "error_code", "duration_ms", "count", "total", "hidden_count", "hidden_suspicious", "hidden_low_quality", "hidden_keyword", "changed_fields"}
        }
        for event_sink in self._event_sinks:
            try:
                await event_sink(
                    definition.code,
                    fields=safe_fields,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    task_id=task_id,
                )
            except Exception:  # noqa: BLE001 - event consumers never break logging
                logger.warning("business event consumer failed")
        legacy_message = _legacy_event_message(event, safe_fields)
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
                category=definition.category,
                message=legacy_message,
                retention_days=retention_days,
                max_file_mb=max_file_mb,
                event_code=definition.code,
                event_version=definition.version,
                title_zh=definition.title_zh,
                message_zh=definition.render(safe_fields, counts),
                suggestion_zh=definition.suggestion_zh if level in {LoggingLevel.WARNING, LoggingLevel.ERROR} else None,
                status=status,
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                task_id=task_id,
                duration_ms=duration_ms,
                counts=counts,
                error_code=error_code,
                context=context,
            )
        except Exception:  # noqa: BLE001 - observability cannot break requests
            logger.warning("business log write failed")

    async def get_inspection(self) -> InspectionSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _inspection_response(settings)

    async def update_inspection(
        self,
        patch: InspectionSettingsPatch,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> InspectionSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != patch.revision:
                raise SettingsConflict
            settings.inspection_auto_start_enabled = patch.auto_start_enabled
            settings.revision += 1
            self._add_audit_record(
                session,
                event="settings.changed",
                fields={
                    "status": "inspection",
                    "changed_fields": ["auto_start_enabled"],
                },
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
            )
            await session.commit()
            response = _inspection_response(settings)
        await self.log_event(
            "settings.changed",
            level=LoggingLevel.INFO,
            fields={"status": "inspection"},
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=request_id,
        )
        return response

    async def get_organization(self) -> OrganizationSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            return _organization_response(settings)

    async def update_organization(
        self,
        patch: OrganizationSettingsPatch,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> OrganizationSettingsResponse:
        async with self._settings_lock, self._session_factory() as session:
            settings = await self._get_or_create(session)
            if settings.revision != patch.revision:
                raise SettingsConflict
            current = _organization_values(settings)
            values = current | patch.model_dump(exclude_unset=True, exclude={"revision"})
            try:
                values = _validate_organization_values(values)
            except ValueError as exc:
                raise OrganizationSettingsValidationError(str(exc)) from None
            settings.organization_settings_json = json.dumps(
                values, ensure_ascii=False, separators=(",", ":")
            )
            settings.revision += 1
            changed = sorted(
                key
                for key, value in values.items()
                if current.get(key) != value
            )
            self._add_audit_record(
                session,
                event="settings.changed",
                fields={
                    "status": "organization",
                    "changed_fields": changed,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
            )
            await session.commit()
            response = _organization_response(settings)
        await self.log_event(
            "settings.changed",
            level=LoggingLevel.INFO,
            fields={"status": "organization"},
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=request_id,
        )
        return response

    @staticmethod
    def _add_audit_record(
        session: AsyncSession,
        *,
        event: str,
        fields: dict[str, object],
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        definition = get_event_definition(event)
        if definition is None:
            raise ValueError(f"unknown audit event: {event}")
        safe_fields = {
            key: value for key, value in fields.items() if key in definition.allowed_fields
        }
        session.add(
            AuditRecord(
                id=uuid4().hex,
                timestamp=utc_now(),
                event_code=definition.code,
                event_version=definition.version,
                title_zh=definition.title_zh,
                message_zh=definition.render(safe_fields),
                suggestion_zh=definition.suggestion_zh,
                status=_safe_optional_text(safe_fields.get("status")),
                actor_type=_safe_scalar(actor_type),
                actor_id=_safe_scalar(actor_id),
                request_id=_safe_scalar(request_id),
                context_json=json.dumps(
                    _safe_context(safe_fields), ensure_ascii=False, separators=(",", ":")
                ),
            )
        )

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
                organization_settings_json=json.dumps(
                    _ORGANIZATION_DEFAULTS, ensure_ascii=False, separators=(",", ":")
                ),
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


_LEVEL_RANK = {
    LoggingLevel.DEBUG: 10,
    LoggingLevel.INFO: 20,
    LoggingLevel.WARNING: 30,
    LoggingLevel.ERROR: 40,
}


def _level_rank(level: LoggingLevel) -> int:
    return _LEVEL_RANK[LoggingLevel(level)]


def _legacy_event_message(event: str, fields: dict[str, object] | None) -> str:
    safe: list[str] = []
    for key, value in sorted((fields or {}).items()):
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


def _organization_values(settings: ApplicationSettings) -> dict[str, object]:
    try:
        raw = json.loads(settings.organization_settings_json or "{}")
    except (TypeError, ValueError):
        raw = {}
    values = dict(_ORGANIZATION_DEFAULTS)
    if isinstance(raw, dict):
        values.update(raw)
    return _validate_organization_values(values)


def _validate_directory_label(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError(f"invalid_{field}")
    label = value.strip()
    if (
        not label
        or len(label) > 240
        or label.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", label)
        or "://" in label
    ):
        raise ValueError(f"invalid_{field}")
    segments = [segment.strip() for segment in label.split("/")]
    if any(
        not segment
        or segment in {".", ".."}
        or _DIRECTORY_LABEL_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ValueError(f"invalid_{field}")
    return "/".join(segments)


def _validate_directory_labels(
    value: object, *, expected_count: int, field: str
) -> list[str]:
    if value is None or value == []:
        return []
    if not isinstance(value, list) or len(value) != expected_count:
        raise ValueError(f"invalid_{field}")
    labels: list[str] = []
    for item in value:
        label = _validate_directory_label(item, field)
        if label is None:
            raise ValueError(f"invalid_{field}")
        labels.append(label)
    return labels


def _validate_organization_values(values: dict[str, object]) -> dict[str, object]:
    result = dict(_ORGANIZATION_DEFAULTS)
    result.update(values)
    sources = result.get("source_directory_ids")
    if not isinstance(sources, list) or any(
        not isinstance(item, str) or _CID_PATTERN.fullmatch(item) is None for item in sources
    ):
        raise ValueError("invalid_source_directory_ids")
    normalized_sources = list(dict.fromkeys(sources))
    source_labels = _validate_directory_labels(
        result.get("source_directory_labels"),
        expected_count=len(normalized_sources),
        field="source_directory_labels",
    )
    target = result.get("target_directory_id")
    if target == "":
        target = None
    if target is not None and (
        not isinstance(target, str) or _CID_PATTERN.fullmatch(target) is None
    ):
        raise ValueError("invalid_target_directory_id")
    if target is not None and target in normalized_sources:
        raise ValueError("source_target_same")
    target_label = _validate_directory_label(
        result.get("target_directory_label"), "target_directory_label"
    )
    if target is None:
        target_label = None
    push_target = result.get("push_directory_id")
    if push_target == "":
        push_target = None
    if push_target is not None and (
        not isinstance(push_target, str) or _CID_PATTERN.fullmatch(push_target) is None
    ):
        raise ValueError("invalid_push_directory_id")
    push_label = _validate_directory_label(
        result.get("push_directory_label"), "push_directory_label"
    )
    if push_target is None:
        push_label = None
    for key in ("video_extensions", "metadata_extensions"):
        extensions = result.get(key)
        if not isinstance(extensions, list) or any(
            not isinstance(item, str)
            or _EXTENSION_PATTERN.fullmatch(item.strip().lower()) is None
            for item in extensions
        ):
            raise ValueError(f"invalid_{key}")
        result[key] = list(dict.fromkeys(item.strip().lower() for item in extensions))
    result["source_directory_ids"] = normalized_sources
    result["source_directory_labels"] = source_labels
    result["target_directory_id"] = target
    result["target_directory_label"] = target_label
    result["push_directory_id"] = push_target
    result["push_directory_label"] = push_label
    if (
        not isinstance(result.get("scan_interval_minutes"), int)
        or isinstance(result["scan_interval_minutes"], bool)
        or not 5 <= result["scan_interval_minutes"] <= 1440
    ):
        raise ValueError("invalid_scan_interval_minutes")
    for key in (
        "schedule_enabled",
        "auto_execute_enabled",
        "rename_enabled",
        "media_probe_enabled",
        "ai_identification_enabled",
        "cleanup_empty_directories",
        "strm_linkage_enabled",
        "include_children_category",
        "include_concert_category",
        "region_grouping_enabled",
        "year_grouping_enabled",
        "prefer_remux",
        "prefer_resolution",
        "prefer_dolby",
        "multi_version_enabled",
    ):
        if not isinstance(result.get(key), bool):
            raise ValueError(f"invalid_{key}")  # noqa: TRY004
    for key, lower, upper in (
        ("small_file_threshold_mb", 0, 10_240),
        ("operation_delay_seconds", 0, 60),
    ):
        value = result.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not lower <= value <= upper
        ):
            raise ValueError(f"invalid_{key}")
    if result.get("conflict_mode") not in (0, 1, 2):
        raise ValueError("invalid_conflict_mode")
    return result


def _organization_response(settings: ApplicationSettings) -> OrganizationSettingsResponse:
    values = _organization_values(settings)
    return OrganizationSettingsResponse(**values, revision=settings.revision)
