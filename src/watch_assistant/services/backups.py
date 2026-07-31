"""Local SQLite online backups with non-sensitive manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from watch_assistant.models import BackupRestoreApproval
from watch_assistant.schemas import (
    BackupConfigurationExportResponse,
    BackupConfigurationImportRequest,
    BackupConfigurationImportResponse,
    BackupListResponse,
    BackupResponse,
    BackupRestoreApprovalResponse,
    BackupRestorePreviewResponse,
    LoggingLevel,
)
from watch_assistant.services.maintenance_gate import (
    MaintenanceDrainIncomplete,
    MaintenanceGate,
    MaintenanceNotActive,
    approval_response,
    encode_drain_report,
    new_approval_id,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.settings import (
    ConfigurationImportConfirmationRequired,
    ConfigurationImportConflict,
    ConfigurationImportValidationError,
)

if TYPE_CHECKING:
    from watch_assistant.services.notifications import NotificationService
    from watch_assistant.services.settings import SettingsService


class BackupServiceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _BackupFiles:
    database: Path
    manifest: Path


@dataclass(frozen=True, slots=True)
class BackupRestoreResult:
    """Result of an offline, confirmed database replacement."""

    backup_id: str
    pre_restore_backup_id: str
    status: Literal["restored"]
    integrity_ok: bool
    restart_required: bool


class BackupService:
    def __init__(
        self,
        database_path: str | None,
        backup_directory: Path,
        *,
        release: str = "unknown",
        retention_count: int = 7,
        event_logger: EventLogger | None = None,
        settings_service: SettingsService | None = None,
        notification_service: NotificationService | None = None,
        session_factory=None,
        maintenance_gate: MaintenanceGate | None = None,
    ) -> None:
        self._database_path = (
            None if database_path in (None, ":memory:") else Path(database_path)
        )
        self._backup_directory = backup_directory
        self._release = release
        self._retention_count = retention_count
        self._event_logger = event_logger
        self._settings_service = settings_service
        self._notification_service = notification_service
        self._session_factory = session_factory
        self._maintenance_gate = maintenance_gate
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

    async def export_configuration(
        self,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> BackupConfigurationExportResponse:
        if self._settings_service is None or self._notification_service is None:
            raise BackupServiceError("backup_configuration_unavailable")
        async with self._lock:
            response = BackupConfigurationExportResponse(
                exported_at=datetime.now(UTC),
                release=self._release,
                logging=await self._settings_service.get_logging(),
                inspection=await self._settings_service.get_inspection(),
                content_policy=await self._settings_service.get_content_policy(),
                organization=await self._settings_service.get_organization(),
                notifications=await self._notification_service.get_preferences(),
                requires_reconfiguration=[
                    "tmdb_api_key",
                    "p115_cookie",
                    "web_password",
                    "agent_token",
                ],
            )
        await emit_event(
            self._event_logger,
            "backup.configuration_exported",
            fields={"status": "ready", "count": 1},
            request_id=request_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return response

    async def import_configuration(
        self,
        payload: BackupConfigurationImportRequest,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> BackupConfigurationImportResponse:
        if self._settings_service is None or self._notification_service is None:
            raise BackupServiceError("backup_configuration_unavailable")
        try:
            response = await self._settings_service.import_configuration(
                payload,
                release=self._release,
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
            )
        except ConfigurationImportConfirmationRequired as exc:
            raise BackupServiceError("backup_configuration_confirmation_required") from exc
        except ConfigurationImportConflict as exc:
            raise BackupServiceError("backup_configuration_conflict") from exc
        except ConfigurationImportValidationError as exc:
            raise BackupServiceError("backup_configuration_invalid") from exc
        await emit_event(
            self._event_logger,
            "backup.configuration_imported",
            fields={"status": "imported", "count": len(response.imported_sections)},
            request_id=request_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return response

    async def request_restore_approval(
        self,
        backup_id: str,
        *,
        confirmed: bool,
        requester_identity: str,
        actor_type: str = "web",
    ) -> BackupRestoreApprovalResponse:
        if confirmed is not True:
            raise BackupServiceError("restore_approval_confirmation_required")
        if self._session_factory is None or self._maintenance_gate is None:
            raise BackupServiceError("restore_approval_unavailable")
        preview = await self.preview_restore(backup_id)
        if preview.status != "ready":
            raise BackupServiceError("restore_preview_not_ready")
        manifest, _source = await asyncio.to_thread(self._load_backup_sync, backup_id)
        maintenance = await self._maintenance_gate.enter(
            reason="database_restore", actor_id=requester_identity
        )
        now = datetime.now(UTC)
        approval = BackupRestoreApproval(
            id=new_approval_id(),
            backup_id=backup_id,
            backup_sha256=manifest.sha256,
            requester_identity=requester_identity,
            status="pending",
            maintenance_generation=maintenance.generation,
            drain_report_json=encode_drain_report(maintenance),
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        async with self._session_factory() as session:
            session.add(approval)
            await session.commit()
        await emit_event(
            self._event_logger,
            "backup.restore_approval_requested",
            level=LoggingLevel.WARNING,
            fields={"status": "pending"},
            actor_type=actor_type,
            actor_id=requester_identity,
            resource_type="backup",
            resource_id=backup_id,
        )
        return approval_response(approval, maintenance)

    async def approve_restore(
        self,
        approval_id: str,
        *,
        confirmed: bool,
        approver_identity: str,
        actor_type: str = "web",
    ) -> BackupRestoreApprovalResponse:
        if confirmed is not True:
            raise BackupServiceError("restore_approval_confirmation_required")
        if self._session_factory is None or self._maintenance_gate is None:
            raise BackupServiceError("restore_approval_unavailable")
        async with self._session_factory() as session:
            approval = await session.get(BackupRestoreApproval, approval_id)
        if approval is None:
            raise BackupServiceError("restore_approval_not_found")
        now = datetime.now(UTC)
        if approval.status != "pending":
            status = await self._maintenance_gate.status()
            return approval_response(approval, status)
        if _as_utc(approval.expires_at) <= now:
            async with self._session_factory() as session:
                current = await session.get(BackupRestoreApproval, approval_id)
                if current is not None and current.status == "pending":
                    current.status = "expired"
                    await session.commit()
                    approval = current
            raise BackupServiceError("restore_approval_expired")
        if approver_identity == approval.requester_identity:
            raise BackupServiceError("restore_second_approver_required")
        preview = await self.preview_restore(approval.backup_id)
        if preview.status != "ready":
            raise BackupServiceError("restore_preview_changed")
        try:
            maintenance = await self._maintenance_gate.require_safe_point()
        except MaintenanceNotActive as exc:
            raise BackupServiceError("maintenance_mode_required") from exc
        except MaintenanceDrainIncomplete as exc:
            raise BackupServiceError("restore_drain_incomplete") from exc
        manifest, _source = await asyncio.to_thread(
            self._load_backup_sync, approval.backup_id
        )
        if manifest.sha256 != approval.backup_sha256:
            raise BackupServiceError("restore_preview_changed")
        async with self._session_factory() as session:
            current = await session.get(BackupRestoreApproval, approval_id)
            if current is None or current.status != "pending":
                raise BackupServiceError("restore_approval_conflict")
            current.status = "approved"
            current.approver_identity = approver_identity
            current.approved_at = now
            current.drain_report_json = encode_drain_report(maintenance)
            await session.commit()
            approval = current
        await emit_event(
            self._event_logger,
            "backup.restore_approved",
            fields={"status": "approved"},
            actor_type=actor_type,
            actor_id=approver_identity,
            resource_type="backup",
            resource_id=approval.backup_id,
        )
        return approval_response(approval, maintenance)

    async def get_restore_approval(
        self, approval_id: str
    ) -> BackupRestoreApprovalResponse:
        if self._session_factory is None or self._maintenance_gate is None:
            raise BackupServiceError("restore_approval_unavailable")
        async with self._session_factory() as session:
            approval = await session.get(BackupRestoreApproval, approval_id)
        if approval is None:
            raise BackupServiceError("restore_approval_not_found")
        status = await self._maintenance_gate.status()
        return approval_response(approval, status)

    async def restore_to(
        self,
        backup_id: str,
        target_path: str | Path,
        *,
        confirmed: bool,
        service_stopped: bool,
    ) -> BackupRestoreResult:
        """Restore a verified backup into a stopped service's database file.

        This is intentionally an offline maintenance operation. It is not
        exposed through the running Web API, because an online process cannot
        prove that its database connections and workers have stopped.
        """

        event_backup_id = (
            backup_id
            if isinstance(backup_id, str)
            and re.fullmatch(r"backup_[0-9a-f]{32}", backup_id)
            else None
        )
        try:
            _validate_backup_id(backup_id)
            if confirmed is not True:
                raise BackupServiceError("confirmation_required")
            if service_stopped is not True:
                raise BackupServiceError("restore_requires_service_stop")
            async with self._lock:
                result = await asyncio.to_thread(
                    self._restore_sync, backup_id, Path(target_path)
                )
        except BackupServiceError as exc:
            await emit_event(
                self._event_logger,
                "backup.restore_failed",
                level=LoggingLevel.ERROR,
                fields={"status": "failed", "error_code": exc.code},
                resource_type="backup",
                resource_id=event_backup_id,
            )
            raise
        except (OSError, sqlite3.Error) as exc:
            await emit_event(
                self._event_logger,
                "backup.restore_failed",
                level=LoggingLevel.ERROR,
                fields={"status": "failed", "error_code": "restore_failed"},
                resource_type="backup",
                resource_id=event_backup_id,
            )
            raise BackupServiceError("restore_failed") from exc
        await emit_event(
            self._event_logger,
            "backup.restore_succeeded",
            fields={"status": "restored", "count": 1},
            resource_type="backup",
            resource_id=backup_id,
        )
        return result

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

    def _create_sync(
        self,
        source_path: Path | None = None,
        *,
        apply_retention: bool = True,
    ) -> BackupResponse:
        source_path = source_path or self._database_path
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
            if apply_retention:
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

    def _restore_sync(
        self, backup_id: str, target_path: Path
    ) -> BackupRestoreResult:
        manifest, source_path = self._load_backup_sync(backup_id)
        _validate_backup_file(manifest, source_path)
        target_path = target_path.resolve()
        backup_directory = self._backup_directory.resolve()
        if (
            not target_path.is_file()
            or target_path == source_path.resolve()
            or backup_directory in target_path.parents
        ):
            raise BackupServiceError("restore_target_invalid")
        current_migrations = set(_read_schema_migrations(target_path))
        backup_migrations = set(manifest.schema_migrations)
        if not current_migrations.issubset(backup_migrations):
            raise BackupServiceError("restore_validation_failed")

        # Keep the pre-restore snapshot until the replacement has been
        # validated. Retention is applied only after a successful restore.
        pre_restore = self._create_sync(target_path, apply_retention=False)
        temporary = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.restore.tmp"
        )
        try:
            shutil.copyfile(source_path, temporary)
            _fsync_file(temporary)
            _remove_sqlite_sidecars(target_path)
            os.replace(temporary, target_path)
            _fsync_directory(target_path.parent)
            if not _sqlite_integrity_ok(target_path):
                raise BackupServiceError("restore_validation_failed")
            self._apply_retention()
            return BackupRestoreResult(
                backup_id=backup_id,
                pre_restore_backup_id=pre_restore.backup_id,
                status="restored",
                integrity_ok=True,
                restart_required=True,
            )
        except BackupServiceError:
            _remove_if_exists(temporary)
            try:
                self._restore_snapshot_sync(pre_restore, target_path)
            except BackupServiceError as rollback_error:
                raise BackupServiceError("restore_rollback_failed") from rollback_error
            raise
        except (OSError, sqlite3.Error) as exc:
            _remove_if_exists(temporary)
            try:
                self._restore_snapshot_sync(pre_restore, target_path)
            except BackupServiceError as rollback_error:
                raise BackupServiceError("restore_rollback_failed") from rollback_error
            raise BackupServiceError("restore_failed") from exc
        finally:
            _remove_if_exists(temporary)

    def _restore_snapshot_sync(
        self, snapshot: BackupResponse, target_path: Path
    ) -> None:
        snapshot_path = self._backup_directory / snapshot.file_name
        _validate_backup_file(snapshot, snapshot_path)
        temporary = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.rollback.tmp"
        )
        try:
            shutil.copyfile(snapshot_path, temporary)
            _fsync_file(temporary)
            _remove_sqlite_sidecars(target_path)
            os.replace(temporary, target_path)
            _fsync_directory(target_path.parent)
            if not _sqlite_integrity_ok(target_path):
                raise BackupServiceError("restore_rollback_failed")
        except BackupServiceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BackupServiceError("restore_rollback_failed") from exc
        finally:
            _remove_if_exists(temporary)

    def _load_backup_sync(self, backup_id: str) -> tuple[BackupResponse, Path]:
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
        return manifest, database_path

    def _preview_restore_sync(self, backup_id: str) -> BackupRestorePreviewResponse:
        manifest, database_path = self._load_backup_sync(backup_id)

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
    try:
        connection = sqlite3.connect(str(path), uri=False)
    except sqlite3.Error:
        return False
    try:
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error:
            return False
        return bool(result and result[0] == "ok")
    finally:
        connection.close()


def _validate_backup_file(manifest: BackupResponse, database_path: Path) -> None:
    try:
        digest_valid = _sha256(database_path) == manifest.sha256
    except OSError as exc:
        raise BackupServiceError("restore_validation_failed") from exc
    if not digest_valid:
        raise BackupServiceError("backup_digest_mismatch")
    try:
        integrity_valid = _sqlite_integrity_ok(database_path)
    except (OSError, sqlite3.Error) as exc:
        raise BackupServiceError("restore_validation_failed") from exc
    if not integrity_valid:
        raise BackupServiceError("backup_integrity_failed")


def _remove_sqlite_sidecars(path: Path) -> None:
    _remove_if_exists(Path(str(path) + "-wal"))
    _remove_if_exists(Path(str(path) + "-shm"))


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
