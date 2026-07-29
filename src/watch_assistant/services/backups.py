"""Local SQLite online backups with non-sensitive manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from watch_assistant.schemas import (
    BackupListResponse,
    BackupResponse,
    BackupRestorePreviewResponse,
    LoggingLevel,
)
from watch_assistant.services.observability import EventLogger, emit_event


class BackupServiceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _BackupFiles:
    database: Path
    manifest: Path


class BackupService:
    def __init__(
        self,
        database_path: str | None,
        backup_directory: Path,
        *,
        release: str = "unknown",
        retention_count: int = 7,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._database_path = (
            None if database_path in (None, ":memory:") else Path(database_path)
        )
        self._backup_directory = backup_directory
        self._release = release
        self._retention_count = retention_count
        self._event_logger = event_logger
        self._lock = asyncio.Lock()

    async def create(self) -> BackupResponse:
        if self._database_path is None:
            raise BackupServiceError("backup_requires_file_database")
        async with self._lock:
            try:
                response = await asyncio.to_thread(self._create_sync)
            except BackupServiceError as exc:
                await emit_event(
                    self._event_logger,
                    "backup.failed",
                    level=LoggingLevel.ERROR,
                    fields={"status": "failed", "error_code": exc.code},
                    error_code=exc.code,
                )
                raise
        await emit_event(
            self._event_logger,
            "backup.created",
            fields={"status": "ready", "count": 1},
        )
        return response

    async def list(self) -> BackupListResponse:
        return await asyncio.to_thread(self._list_sync)

    async def preview_restore(self, backup_id: str) -> BackupRestorePreviewResponse:
        _validate_backup_id(backup_id)
        try:
            response = await asyncio.to_thread(self._preview_restore_sync, backup_id)
        except BackupServiceError as exc:
            await emit_event(
                self._event_logger,
                "backup.restore_preview",
                level=LoggingLevel.WARNING,
                fields={"status": "invalid", "error_code": exc.code},
                error_code=exc.code,
            )
            raise
        await emit_event(
            self._event_logger,
            "backup.restore_preview",
            level=LoggingLevel.WARNING if response.status != "ready" else LoggingLevel.INFO,
            fields={"status": response.status},
        )
        return response

    def _create_sync(self) -> BackupResponse:
        source_path = self._database_path
        if source_path is None:
            raise BackupServiceError("backup_requires_file_database")
        if not source_path.is_file():
            raise BackupServiceError("database_not_found")
        self._backup_directory.mkdir(parents=True, exist_ok=True)
        backup_id = "backup_" + uuid4().hex
        files = _BackupFiles(
            database=self._backup_directory / f"{backup_id}.db",
            manifest=self._backup_directory / f"{backup_id}.json",
        )
        temporary = self._backup_directory / f".{backup_id}.db.tmp"
        try:
            self._online_backup(source_path, temporary)
            os.replace(temporary, files.database)
            digest = _sha256(files.database)
            migrations = _read_schema_migrations(files.database)
            created_at = datetime.now(UTC)
            response = BackupResponse(
                backup_id=backup_id,
                created_at=created_at,
                file_name=files.database.name,
                size_bytes=files.database.stat().st_size,
                sha256=digest,
                schema_migrations=migrations,
                release=self._release,
            )
            _atomic_write_json(files.manifest, response.model_dump(mode="json"))
            self._apply_retention()
            return response
        except BackupServiceError:
            _remove_if_exists(temporary)
            _remove_if_exists(files.database)
            _remove_if_exists(files.manifest)
            raise
        except (OSError, sqlite3.Error) as exc:
            _remove_if_exists(temporary)
            _remove_if_exists(files.database)
            _remove_if_exists(files.manifest)
            raise BackupServiceError("backup_failed") from exc

    @staticmethod
    def _online_backup(source_path: Path, temporary: Path) -> None:
        source = sqlite3.connect(str(source_path), uri=False)
        target = sqlite3.connect(str(temporary), uri=False)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

    def _list_sync(self) -> BackupListResponse:
        if not self._backup_directory.is_dir():
            return BackupListResponse(items=[])
        items: list[BackupResponse] = []
        for manifest in self._backup_directory.glob("backup_*.json"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                item = BackupResponse.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            database = self._backup_directory / item.file_name
            if database.is_file():
                items.append(item)
        items.sort(key=lambda item: item.created_at, reverse=True)
        return BackupListResponse(items=items)

    def _preview_restore_sync(self, backup_id: str) -> BackupRestorePreviewResponse:
        if not self._backup_directory.is_dir():
            raise BackupServiceError("backup_not_found")
        manifest_path = self._backup_directory / f"{backup_id}.json"
        if not manifest_path.is_file():
            raise BackupServiceError("backup_not_found")
        try:
            manifest = BackupResponse.model_validate(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise BackupServiceError("backup_manifest_invalid") from exc
        if manifest.backup_id != backup_id or Path(manifest.file_name).name != manifest.file_name:
            raise BackupServiceError("backup_manifest_invalid")
        database_path = self._backup_directory / manifest.file_name
        if not database_path.is_file():
            raise BackupServiceError("backup_database_missing")

        sha256_valid = False
        integrity_ok = False
        try:
            sha256_valid = _sha256(database_path) == manifest.sha256
            integrity_ok = _sqlite_integrity_ok(database_path)
        except (OSError, sqlite3.Error):
            pass
        current_migrations = set(
            _read_schema_migrations(self._database_path)
            if self._database_path is not None and self._database_path.is_file()
            else []
        )
        backup_migrations = set(manifest.schema_migrations)
        missing_migrations = sorted(current_migrations - backup_migrations)
        migration_compatible = not missing_migrations
        status: str = "ready"
        if not sha256_valid or not integrity_ok:
            status = "invalid"
        elif not migration_compatible:
            status = "incompatible"
        return BackupRestorePreviewResponse(
            backup_id=backup_id,
            status=status,
            sha256_valid=sha256_valid,
            integrity_ok=integrity_ok,
            migration_compatible=migration_compatible,
            backup_release=manifest.release,
            current_release=self._release,
            missing_migrations=missing_migrations,
            requires_reconfiguration=[
                "p115_cookie",
                "tmdb_api_key",
                "web_password",
                "agent_token",
            ],
            warnings=(
                ["restore_requires_service_stop_and_confirmation"]
                if status == "ready"
                else ["restore_is_blocked_until_backup_validation_passes"]
            ),
        )

    def _apply_retention(self) -> None:
        manifests = list(self._backup_directory.glob("backup_*.json"))
        manifests.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for manifest in manifests[self._retention_count :]:
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                file_name = payload.get("file_name")
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(file_name, str) and Path(file_name).name == file_name:
                _remove_if_exists(self._backup_directory / file_name)
            _remove_if_exists(manifest)


def _read_schema_migrations(database_path: Path) -> list[str]:
    connection = sqlite3.connect(str(database_path), uri=False)
    try:
        try:
            rows = connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [row[0] for row in rows if isinstance(row[0], str)]
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(str(path), uri=False)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(result and result[0] == "ok")
    finally:
        connection.close()


def _validate_backup_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"backup_[0-9a-f]{32}", value):
        raise BackupServiceError("invalid_backup_id")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
