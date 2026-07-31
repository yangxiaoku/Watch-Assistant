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

from cryptography.fernet import Fernet, InvalidToken

from watch_assistant.models import BackupRestoreApproval
from watch_assistant.schemas import (
    BackupConfigurationExportResponse,
    BackupConfigurationImportRequest,
    BackupConfigurationImportResponse,
    BackupDeleteResponse,
    BackupListResponse,
    BackupResponse,
    BackupRestoreApprovalResponse,
    BackupRestorePreviewResponse,
    EncryptedBackupResponse,
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
    business_consistency_ok: bool = True


@dataclass(frozen=True, slots=True)
class _BusinessConsistency:
    ok: bool
    checks: tuple[dict[str, object], ...]
    digest: str


@dataclass(frozen=True, slots=True)
class _RestoreAuthorization:
    approval_id: str
    journal_path: Path


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
        encrypted_backup_enabled: bool = False,
        encrypted_backup_destination: Path | None = None,
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
        self._encrypted_backup_enabled = encrypted_backup_enabled
        self._encrypted_backup_destination = encrypted_backup_destination
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

    async def delete(
        self,
        backup_id: str,
        *,
        confirmed: bool,
    ) -> BackupDeleteResponse:
        if confirmed is not True:
            raise BackupServiceError("backup_delete_confirmation_required")
        async with self._lock:
            try:
                response = await asyncio.to_thread(self._delete_sync, backup_id)
            except BackupServiceError as exc:
                await emit_event(
                    self._event_logger,
                    "backup.delete_failed",
                    level=LoggingLevel.ERROR,
                    fields={"status": "failed", "error_code": exc.code},
                    resource_type="backup",
                    resource_id=backup_id if _is_backup_id(backup_id) else None,
                )
                raise
        await emit_event(
            self._event_logger,
            "backup.deleted",
            fields={"status": "deleted", "count": 1},
            resource_type="backup",
            resource_id=backup_id,
        )
        return response

    async def create_encrypted_copy(
        self,
        backup_id: str,
        *,
        recovery_key: str,
    ) -> EncryptedBackupResponse:
        if not self._encrypted_backup_enabled or self._encrypted_backup_destination is None:
            raise BackupServiceError("encrypted_backup_disabled")
        async with self._lock:
            try:
                response = await asyncio.to_thread(
                    self._create_encrypted_copy_sync,
                    backup_id,
                    recovery_key,
                )
            except BackupServiceError as exc:
                await emit_event(
                    self._event_logger,
                    "backup.encrypted_copy_failed",
                    level=LoggingLevel.ERROR,
                    fields={"status": "failed", "error_code": exc.code},
                    resource_type="backup",
                    resource_id=backup_id if _is_backup_id(backup_id) else None,
                )
                raise
        await emit_event(
            self._event_logger,
            "backup.encrypted_copy_created",
            fields={"status": "ready", "count": 1},
            resource_type="backup",
            resource_id=backup_id,
        )
        return response

    async def restore_encrypted_to(
        self,
        encrypted_manifest_path: str | Path,
        target_path: str | Path,
        *,
        recovery_key: str,
        confirmed: bool,
        service_stopped: bool,
        approval_id: str | None = None,
    ) -> BackupRestoreResult:
        if confirmed is not True:
            raise BackupServiceError("confirmation_required")
        if service_stopped is not True:
            raise BackupServiceError("restore_requires_service_stop")
        try:
            async with self._lock:
                result = await asyncio.to_thread(
                    self._restore_encrypted_sync,
                    Path(encrypted_manifest_path),
                    Path(target_path),
                    recovery_key,
                    approval_id,
                )
        except BackupServiceError as exc:
            await emit_event(
                self._event_logger,
                "backup.restore_failed",
                level=LoggingLevel.ERROR,
                fields={"status": "failed", "error_code": exc.code},
                resource_type="backup",
            )
            raise
        await emit_event(
            self._event_logger,
            "backup.restore_succeeded",
            fields={"status": "restored", "count": 1},
            resource_type="backup",
            resource_id=result.backup_id,
        )
        return result

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
        approval_id: str | None = None,
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
                    self._restore_sync,
                    backup_id,
                    Path(target_path),
                    approval_id,
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

    def _create_encrypted_copy_sync(
        self, backup_id: str, recovery_key: str
    ) -> EncryptedBackupResponse:
        _validate_backup_id(backup_id)
        if self._encrypted_backup_destination is None:
            raise BackupServiceError("encrypted_backup_disabled")
        manifest, source_path = self._load_backup_sync(backup_id)
        _validate_backup_file(manifest, source_path)
        if _business_consistency(source_path).ok is False:
            raise BackupServiceError("encrypted_backup_invalid")
        fernet = _fernet_for_recovery_key(recovery_key)
        try:
            plaintext = source_path.read_bytes()
            ciphertext = fernet.encrypt(plaintext)
        except OSError as exc:
            raise BackupServiceError("encrypted_backup_failed") from exc
        destination = self._encrypted_backup_destination
        destination.mkdir(parents=True, exist_ok=True)
        file_name = f"encrypted_{backup_id}.db.enc"
        manifest_file_name = f"encrypted_{backup_id}.json"
        ciphertext_path = destination / file_name
        encrypted_manifest_path = destination / manifest_file_name
        ciphertext_sha256 = hashlib.sha256(ciphertext).hexdigest()
        _atomic_write_bytes(ciphertext_path, ciphertext)
        encrypted_manifest = EncryptedBackupResponse(
            source_backup_id=backup_id,
            created_at=datetime.now(UTC),
            file_name=file_name,
            manifest_file_name=manifest_file_name,
            size_bytes=len(ciphertext),
            ciphertext_sha256=ciphertext_sha256,
            plaintext_sha256=manifest.sha256,
            schema_migrations=manifest.schema_migrations,
            release=manifest.release,
            validation_status="verified",
        )
        _atomic_write_json(
            encrypted_manifest_path,
            encrypted_manifest.model_dump(mode="json"),
        )
        return encrypted_manifest

    def _restore_encrypted_sync(
        self,
        encrypted_manifest_path: Path,
        target_path: Path,
        recovery_key: str,
        approval_id: str | None,
    ) -> BackupRestoreResult:
        encrypted_manifest, ciphertext_path = _load_encrypted_manifest(
            encrypted_manifest_path
        )
        fernet = _fernet_for_recovery_key(recovery_key)
        try:
            ciphertext = ciphertext_path.read_bytes()
        except OSError as exc:
            raise BackupServiceError("encrypted_backup_invalid") from exc
        if hashlib.sha256(ciphertext).hexdigest() != encrypted_manifest.ciphertext_sha256:
            raise BackupServiceError("encrypted_backup_invalid")
        try:
            plaintext = fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise BackupServiceError("encrypted_backup_key_invalid") from exc
        if hashlib.sha256(plaintext).hexdigest() != encrypted_manifest.plaintext_sha256:
            raise BackupServiceError("encrypted_backup_invalid")
        temporary = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.encrypted.tmp"
        )
        try:
            _atomic_write_bytes(temporary, plaintext)
            if not _sqlite_integrity_ok(temporary) or not _business_consistency(temporary).ok:
                raise BackupServiceError("encrypted_backup_invalid")
            source_manifest = BackupResponse(
                backup_id=encrypted_manifest.source_backup_id,
                created_at=encrypted_manifest.created_at,
                file_name=f"{encrypted_manifest.source_backup_id}.db",
                size_bytes=len(plaintext),
                sha256=encrypted_manifest.plaintext_sha256,
                schema_migrations=encrypted_manifest.schema_migrations,
                release=encrypted_manifest.release,
            )
            return self._restore_sync(
                encrypted_manifest.source_backup_id,
                target_path,
                approval_id,
                source=(source_manifest, temporary),
            )
        finally:
            _remove_if_exists(temporary)

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
            return BackupListResponse(items=[], retention_count=self._retention_count)
        items: list[BackupResponse] = []
        for manifest in self._backup_directory.glob("backup_*.json"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                item = BackupResponse.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            database = self._backup_directory / item.file_name
            validation_status = _backup_validation_status(item, database)
            items.append(
                item.model_copy(update={"validation_status": validation_status})
            )
        items.sort(key=lambda item: item.created_at, reverse=True)
        ranked = [
            item.model_copy(
                update={
                    "retention_rank": index,
                    "retained": index <= self._retention_count,
                }
            )
            for index, item in enumerate(items, start=1)
        ]
        return BackupListResponse(
            items=ranked,
            retention_count=self._retention_count,
        )

    def _delete_sync(self, backup_id: str) -> BackupDeleteResponse:
        _validate_backup_id(backup_id)
        manifest, database = self._load_backup_sync(backup_id)
        del manifest
        current = self._list_sync()
        if len(current.items) <= 1:
            raise BackupServiceError("backup_delete_last")
        manifest_path = self._backup_directory / f"{backup_id}.json"
        tombstone = self._backup_directory / f".{backup_id}.{uuid4().hex}.delete"
        tombstone_manifest = tombstone.with_suffix(".json")
        tombstone_database = tombstone.with_suffix(".db")
        moved: list[tuple[Path, Path]] = []
        try:
            os.replace(manifest_path, tombstone_manifest)
            moved.append((manifest_path, tombstone_manifest))
            os.replace(database, tombstone_database)
            moved.append((database, tombstone_database))
            _fsync_directory(self._backup_directory)
            _remove_if_exists(tombstone_manifest)
            _remove_if_exists(tombstone_database)
            _fsync_directory(self._backup_directory)
        except (OSError, sqlite3.Error) as exc:
            for original, temporary in reversed(moved):
                if temporary.exists() and not original.exists():
                    try:
                        os.replace(temporary, original)
                    except OSError:
                        pass
            raise BackupServiceError("backup_delete_failed") from exc
        return BackupDeleteResponse(backup_id=backup_id)

    def _restore_sync(
        self,
        backup_id: str,
        target_path: Path,
        approval_id: str | None = None,
        *,
        source: tuple[BackupResponse, Path] | None = None,
    ) -> BackupRestoreResult:
        if source is None:
            manifest, source_path = self._load_backup_sync(backup_id)
        else:
            manifest, source_path = source
        _validate_backup_file(manifest, source_path)
        source_consistency = _business_consistency(source_path)
        if not source_consistency.ok:
            raise BackupServiceError("restore_consistency_failed")
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
        authorization = self._validate_restore_authorization_sync(
            approval_id, backup_id, manifest, target_path
        )

        # Keep the pre-restore snapshot until the replacement has been
        # validated. Retention is applied only after a successful restore.
        pre_restore = self._create_sync(target_path, apply_retention=False)
        temporary = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.restore.tmp"
        )
        journal_written = False
        try:
            if authorization is not None:
                _write_restore_journal(
                    authorization.journal_path,
                    {
                        "approval_id": authorization.approval_id,
                        "backup_id": backup_id,
                        "backup_sha256": manifest.sha256,
                        "status": "armed",
                    },
                )
                journal_written = True
            shutil.copyfile(source_path, temporary)
            _fsync_file(temporary)
            _remove_sqlite_sidecars(target_path)
            os.replace(temporary, target_path)
            _fsync_directory(target_path.parent)
            if not _sqlite_integrity_ok(target_path):
                raise BackupServiceError("restore_validation_failed")
            target_consistency = _business_consistency(target_path)
            if (
                not target_consistency.ok
                or target_consistency.digest != source_consistency.digest
            ):
                raise BackupServiceError("restore_consistency_failed")
            if authorization is not None:
                _write_restore_journal(
                    authorization.journal_path,
                    {
                        "approval_id": authorization.approval_id,
                        "backup_id": backup_id,
                        "backup_sha256": manifest.sha256,
                        "status": "consumed",
                    },
                )
            self._apply_retention()
            return BackupRestoreResult(
                backup_id=backup_id,
                pre_restore_backup_id=pre_restore.backup_id,
                status="restored",
                integrity_ok=True,
                restart_required=True,
                business_consistency_ok=True,
            )
        except BackupServiceError:
            _remove_if_exists(temporary)
            try:
                self._restore_snapshot_sync(pre_restore, target_path)
            except BackupServiceError as rollback_error:
                raise BackupServiceError("restore_rollback_failed") from rollback_error
            if journal_written and authorization is not None:
                _remove_if_exists(authorization.journal_path)
            raise
        except (OSError, sqlite3.Error) as exc:
            _remove_if_exists(temporary)
            try:
                self._restore_snapshot_sync(pre_restore, target_path)
            except BackupServiceError as rollback_error:
                raise BackupServiceError("restore_rollback_failed") from rollback_error
            if journal_written and authorization is not None:
                _remove_if_exists(authorization.journal_path)
            raise BackupServiceError("restore_failed") from exc
        finally:
            _remove_if_exists(temporary)

    def _validate_restore_authorization_sync(
        self,
        approval_id: str | None,
        backup_id: str,
        manifest: BackupResponse,
        target_path: Path,
    ) -> _RestoreAuthorization | None:
        connection = sqlite3.connect(str(target_path), uri=False)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            journal_path = self._backup_directory / f".restore-approval-{approval_id}.json"
            if approval_id is not None and journal_path.exists():
                raise BackupServiceError("restore_approval_conflict")
            if "backup_restore_approvals" not in tables:
                if approval_id is not None:
                    raise BackupServiceError("restore_approval_invalid")
                return None
            if approval_id is None:
                raise BackupServiceError("restore_approval_required")
            _validate_restore_approval_id(approval_id)
            row = connection.execute(
                "SELECT backup_id, backup_sha256, requester_identity, "
                "approver_identity, status, maintenance_generation, expires_at "
                "FROM backup_restore_approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise BackupServiceError("restore_approval_not_found")
            (
                approved_backup_id,
                approved_sha256,
                requester_identity,
                approver_identity,
                status,
                maintenance_generation,
                expires_at,
            ) = row
            if status != "approved":
                raise BackupServiceError("restore_approval_conflict")
            if _database_datetime(expires_at) <= datetime.now(UTC):
                raise BackupServiceError("restore_approval_expired")
            if (
                approved_backup_id != backup_id
                or approved_sha256 != manifest.sha256
                or not requester_identity
                or not approver_identity
                or requester_identity == approver_identity
            ):
                raise BackupServiceError("restore_approval_invalid")
            if "online_maintenance_state" not in tables:
                raise BackupServiceError("restore_approval_invalid")
            state = connection.execute(
                "SELECT active, generation FROM online_maintenance_state WHERE id = ?",
                ("default",),
            ).fetchone()
            if (
                state is None
                or not bool(state[0])
                or int(state[1]) != int(maintenance_generation)
            ):
                raise BackupServiceError("restore_approval_invalid")
        finally:
            connection.close()

        journal_path = self._backup_directory / f".restore-approval-{approval_id}.json"
        return _RestoreAuthorization(approval_id=approval_id, journal_path=journal_path)

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
            if not _business_consistency(target_path).ok:
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
        consistency = _BusinessConsistency(False, (), "")
        try:
            sha256_valid = _sha256(database_path) == manifest.sha256
            integrity_ok = _sqlite_integrity_ok(database_path)
            if integrity_ok:
                consistency = _business_consistency(database_path)
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
        if not sha256_valid or not integrity_ok or not consistency.ok:
            status = "invalid"
        elif not migration_compatible:
            status = "incompatible"
        return BackupRestorePreviewResponse(
            backup_id=backup_id,
            status=status,
            sha256_valid=sha256_valid,
            integrity_ok=integrity_ok,
            migration_compatible=migration_compatible,
            business_consistency_ok=consistency.ok,
            backup_release=manifest.release,
            current_release=self._release,
            missing_migrations=missing_migrations,
            consistency_checks=list(consistency.checks),
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


def _business_consistency(path: Path) -> _BusinessConsistency:
    """Check business references without returning user data or credentials."""

    checks: list[dict[str, object]] = []
    tables: set[str] = set()
    digest = ""
    try:
        connection = sqlite3.connect(str(path), uri=False)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return _BusinessConsistency(False, (), "")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(_check("foreign_keys", len(fk_errors), "外键引用完整"))

        relations = (
            ("inspection_items_batch", "inspection_items", "batch_id", "inspection_batches", "id"),
            ("inspection_items_resource", "inspection_items", "resource_id", "resources", "id"),
            ("subscription_observations_subscription", "subscription_resource_observations", "subscription_id", "subscriptions", "id"),
            ("subscription_observations_resource", "subscription_resource_observations", "resource_id", "resources", "id"),
            ("tasks_resource", "tasks", "resource_id", "resources", "id"),
            ("organization_operations_plan", "organization_operations", "plan_id", "organization_plans", "id"),
            ("organization_operations_workflow", "organization_operations", "workflow_id", "workflows", "id"),
            ("dirty_events_operation", "directory_dirty_events", "operation_id", "organization_operations", "id"),
            ("dirty_generations_operation", "directory_dirty_generations", "operation_id", "organization_operations", "id"),
            ("dirty_generations_library", "directory_dirty_generations", "library_id", "media_libraries", "id"),
            ("strm_library", "strm_manifest_entries", "library_id", "media_libraries", "id"),
            ("inventory_library", "library_inventory_events", "library_id", "media_libraries", "id"),
            ("media_identity_library", "library_media_identities", "library_id", "media_libraries", "id"),
        )
        for name, child, child_key, parent, parent_key in relations:
            if child not in tables or parent not in tables:
                checks.append(_check(name, None, "未启用该业务表，已跳过"))
                continue
            orphan_count = connection.execute(
                f"SELECT COUNT(*) FROM {child} AS child "
                f"WHERE child.{child_key} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} AS parent WHERE parent.{parent_key} = child.{child_key})"
            ).fetchone()[0]
            checks.append(_check(name, int(orphan_count), "业务引用完整"))

        if "tasks" in tables:
            allowed_states = (
                "queued", "submitting", "accepted", "needs_auth", "failed", "uncertain", "cancelled"
            )
            placeholders = ",".join("?" for _ in allowed_states)
            invalid_states = connection.execute(
                f"SELECT COUNT(*) FROM tasks WHERE status NOT IN ({placeholders})",
                allowed_states,
            ).fetchone()[0]
            checks.append(_check("task_states", int(invalid_states), "任务状态值有效"))

        if "subscriptions" in tables:
            duplicate_subscriptions = connection.execute(
                "SELECT COUNT(*) FROM (SELECT tmdb_id, media_type, season_number, "
                "episode_start, episode_end, COUNT(*) AS item_count FROM subscriptions "
                "GROUP BY tmdb_id, media_type, season_number, episode_start, episode_end "
                "HAVING item_count > 1)"
            ).fetchone()[0]
            checks.append(_check("subscription_scope_unique", int(duplicate_subscriptions), "订阅范围唯一"))

        if "strm_manifest_entries" in tables:
            duplicate_strm = connection.execute(
                "SELECT COUNT(*) FROM (SELECT library_id, cloud_file_id, COUNT(*) AS item_count "
                "FROM strm_manifest_entries WHERE is_current = 1 "
                "GROUP BY library_id, cloud_file_id HAVING item_count > 1)"
            ).fetchone()[0]
            checks.append(_check("strm_current_unique", int(duplicate_strm), "当前 STRM 身份唯一"))
    except sqlite3.Error:
        checks.append(_check("business_queries", 1, "业务一致性查询失败"))
    try:
        digest = _business_digest(connection, tables)
    except sqlite3.Error:
        checks.append(_check("business_digest", 1, "业务摘要计算失败"))
    finally:
        connection.close()
    return _BusinessConsistency(
        all(item["status"] != "failed" for item in checks), tuple(checks), digest
    )


def _business_digest(connection: sqlite3.Connection, tables: set[str]) -> str:
    digest = hashlib.sha256()
    for table in sorted(item for item in tables if not item.startswith("sqlite_")):
        quoted_table = '"' + table.replace('"', '""') + '"'
        digest.update(table.encode("utf-8"))
        columns = [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        ]
        digest.update(json.dumps(columns, ensure_ascii=False).encode("utf-8"))
        for row in connection.execute(
            f"SELECT * FROM {quoted_table} ORDER BY rowid"
        ):
            values = [
                value.hex() if isinstance(value, bytes) else value for value in row
            ]
            digest.update(
                json.dumps(values, ensure_ascii=False, default=str).encode("utf-8")
            )
    return digest.hexdigest()


def _check(name: str, failures: int | None, message: str) -> dict[str, object]:
    if failures is None:
        return {"name": name, "status": "skipped", "count": 0, "message_zh": message}
    return {
        "name": name,
        "status": "passed" if failures == 0 else "failed",
        "count": failures,
        "message_zh": message,
    }


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
    if not _is_backup_id(value):
        raise BackupServiceError("invalid_backup_id")


def _is_backup_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"backup_[0-9a-f]{32}", value) is not None


def _backup_validation_status(manifest: BackupResponse, database_path: Path) -> str:
    if not database_path.is_file():
        return "missing"
    try:
        if _sha256(database_path) != manifest.sha256:
            return "invalid"
        if not _sqlite_integrity_ok(database_path):
            return "invalid"
        if not _business_consistency(database_path).ok:
            return "invalid"
    except (OSError, sqlite3.Error):
        return "invalid"
    return "verified"


def _fernet_for_recovery_key(value: str) -> Fernet:
    try:
        return Fernet(value.encode("ascii"))
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise BackupServiceError("encrypted_backup_key_invalid") from exc


def _load_encrypted_manifest(
    manifest_path: Path,
) -> tuple[EncryptedBackupResponse, Path]:
    if not manifest_path.is_file():
        raise BackupServiceError("encrypted_backup_invalid")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = EncryptedBackupResponse.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupServiceError("encrypted_backup_invalid") from exc
    if (
        Path(manifest.file_name).name != manifest.file_name
        or Path(manifest.manifest_file_name).name != manifest.manifest_file_name
        or manifest.manifest_file_name != manifest_path.name
        or not _is_backup_id(manifest.source_backup_id)
    ):
        raise BackupServiceError("encrypted_backup_invalid")
    ciphertext_path = manifest_path.parent / manifest.file_name
    if not ciphertext_path.is_file():
        raise BackupServiceError("encrypted_backup_invalid")
    return manifest, ciphertext_path


def _validate_restore_approval_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"restore_[0-9a-f]{32}", value):
        raise BackupServiceError("restore_approval_invalid")


def _database_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            pass
    raise BackupServiceError("restore_approval_invalid")


def _write_restore_journal(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, payload)
    _fsync_file(path)
    _fsync_directory(path.parent)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        _fsync_file(temporary)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        _remove_if_exists(temporary)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
