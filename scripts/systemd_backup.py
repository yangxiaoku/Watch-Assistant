#!/usr/bin/env python3
"""Create and inspect systemd SQLite backups without implicit restoration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
if SRC_ROOT.is_dir():
    import sys

    sys.path.insert(0, str(SRC_ROOT))

from watch_assistant.release_metadata import normalize_full_release, read_release_commit

_MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_RESTORE_POLICY = "preview_or_explicit_manual_command_only"
_BACKUP_ID_PATTERN = re.compile(
    r"^backup_[0-9a-f]{32}$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SystemdBackupError(ValueError):
    """A stable, non-sensitive backup failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as stream:
        stream.flush()
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


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _integrity_ok(path: Path) -> bool:
    try:
        connection = sqlite3.connect(str(path), uri=False)
    except sqlite3.Error:
        return False
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(result and result[0] == "ok")
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _schema_migrations(path: Path) -> list[str]:
    connection = sqlite3.connect(str(path), uri=False)
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


def _validate_manifest_payload(payload: object) -> tuple[str, str, int, str]:
    if not isinstance(payload, dict):
        raise SystemdBackupError("manifest_invalid")
    if payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise SystemdBackupError("manifest_invalid")

    backup_id = payload.get("backup_id")
    database_name = payload.get("database_file")
    created_at = payload.get("created_at")
    size_bytes = payload.get("size_bytes")
    expected_digest = payload.get("sha256")
    release = normalize_full_release(payload.get("release"))
    migrations = payload.get("schema_migrations")
    if (
        not isinstance(backup_id, str)
        or _BACKUP_ID_PATTERN.fullmatch(backup_id) is None
        or not isinstance(database_name, str)
        or Path(database_name).name != database_name
        or database_name != f"{backup_id}.db"
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or not isinstance(expected_digest, str)
        or _SHA256_PATTERN.fullmatch(expected_digest) is None
        or release is None
        or not isinstance(migrations, list)
        or any(not isinstance(item, str) for item in migrations)
        or payload.get("integrity_check") != "ok"
        or payload.get("restore") != _MANIFEST_RESTORE_POLICY
    ):
        raise SystemdBackupError("manifest_invalid")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise SystemdBackupError("manifest_invalid") from exc
    return backup_id, database_name, size_bytes, release


def _release_from_inputs(release: str | None, version_file: Path | None) -> str:
    value = normalize_full_release(release)
    if value is None and version_file is not None:
        value = normalize_full_release(read_release_commit(version_file))
    if value is None:
        raise SystemdBackupError("full_release_required")
    return value


def create_backup(
    *,
    database: Path,
    output_dir: Path,
    release: str,
    retention_count: int = 7,
    created_at: datetime | None = None,
) -> dict[str, object]:
    if not database.is_file():
        raise SystemdBackupError("database_not_found")
    normalized_release = _release_from_inputs(release, None)
    if retention_count < 1:
        raise SystemdBackupError("retention_invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    backup_id = "backup_" + uuid.uuid4().hex
    database_name = f"{backup_id}.db"
    manifest_name = f"{backup_id}.json"
    target = output_dir / database_name
    manifest = output_dir / manifest_name
    temporary = output_dir / f".{database_name}.{uuid.uuid4().hex}.tmp"
    try:
        source = sqlite3.connect(str(database), uri=False)
        destination = sqlite3.connect(str(temporary), uri=False)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        if not _integrity_ok(temporary):
            raise SystemdBackupError("backup_integrity_failed")
        digest = _sha256(temporary)
        migrations = _schema_migrations(temporary)
        size_bytes = temporary.stat().st_size
        _fsync_file(temporary)
        os.replace(temporary, target)
        _fsync_directory(output_dir)
        payload: dict[str, object] = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "backup_id": backup_id,
            "created_at": timestamp.isoformat(),
            "database_file": database_name,
            "size_bytes": size_bytes,
            "sha256": digest,
            "release": normalized_release,
            "schema_migrations": migrations,
            "integrity_check": "ok",
            "restore": _MANIFEST_RESTORE_POLICY,
        }
        _atomic_write_json(manifest, payload)
        _apply_retention(output_dir, retention_count)
        return payload
    except SystemdBackupError:
        _remove(temporary)
        _remove(target)
        _remove(manifest)
        raise
    except (OSError, sqlite3.Error) as exc:
        _remove(temporary)
        _remove(target)
        _remove(manifest)
        raise SystemdBackupError("backup_failed") from exc


def _apply_retention(output_dir: Path, retention_count: int) -> None:
    records: list[tuple[str, Path, Path]] = []
    for manifest_path in output_dir.glob("watch-assistant-*.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            database_name = payload["database_file"]
            created_at = payload["created_at"]
            if (
                not isinstance(database_name, str)
                or Path(database_name).name != database_name
                or not isinstance(created_at, str)
            ):
                continue
            database_path = output_dir / database_name
            if database_path.is_file():
                records.append((created_at, manifest_path, database_path))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            continue
    records.sort(key=lambda item: item[0], reverse=True)
    for _, manifest_path, database_path in records[retention_count:]:
        _remove(database_path)
        _remove(manifest_path)


def inspect_backup(
    *, manifest: Path, current_database: Path | None = None
) -> dict[str, object]:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemdBackupError("manifest_invalid") from exc
    backup_id, database_name, expected_size, _release = _validate_manifest_payload(
        payload
    )
    database = manifest.parent / database_name
    if not database.is_file():
        raise SystemdBackupError("backup_database_missing")
    try:
        size_valid = database.stat().st_size == expected_size
        sha256_valid = _sha256(database) == payload["sha256"]
        integrity_ok = (
            size_valid and sha256_valid and _integrity_ok(database)
        )
    except OSError as exc:
        raise SystemdBackupError("backup_database_unreadable") from exc
    current_migrations = set(
        _schema_migrations(current_database)
        if current_database is not None and current_database.is_file()
        else []
    )
    backup_migrations = set(payload["schema_migrations"])
    missing_migrations = sorted(current_migrations - backup_migrations)
    status = "ready"
    if not sha256_valid or not integrity_ok:
        status = "invalid"
    elif missing_migrations:
        status = "incompatible"
    return {
        "status": status,
        "backup_id": backup_id,
        "size_valid": size_valid,
        "sha256_valid": sha256_valid,
        "integrity_ok": integrity_ok,
        "migration_compatible": not missing_migrations,
        "missing_migrations": missing_migrations,
        "restore_requires_explicit_confirmation": True,
        "database_file": database.name,
    }


def restore_backup(
    *, manifest: Path, database: Path, confirmed: bool, service_stopped: bool
) -> dict[str, object]:
    if confirmed is not True:
        raise SystemdBackupError("restore_requires_explicit_confirmation")
    if service_stopped is not True:
        raise SystemdBackupError("restore_requires_service_stop")
    preview = inspect_backup(manifest=manifest, current_database=database)
    if preview["status"] != "ready":
        raise SystemdBackupError("restore_preview_not_ready")
    source = manifest.parent / str(preview["database_file"])
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.parent / f".{database.name}.{uuid.uuid4().hex}.restore.tmp"
    previous = database.parent / f".{database.name}.{uuid.uuid4().hex}.pre-restore.db"
    try:
        if database.is_file():
            shutil.copy2(database, previous)
        shutil.copy2(source, temporary)
        _fsync_file(temporary)
        os.replace(temporary, database)
        if not _integrity_ok(database):
            raise SystemdBackupError("restored_database_integrity_failed")
    except Exception:
        _remove(temporary)
        if previous.is_file():
            os.replace(previous, database)
        raise
    finally:
        _remove(temporary)
        _remove(previous)
    return {
        "status": "restored",
        "backup_id": preview["backup_id"],
        "integrity_ok": True,
        "manual_command": True,
    }


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--release")
    create.add_argument("--version-file", type=Path)
    create.add_argument("--retention", type=int, default=7)
    preview = subparsers.add_parser("restore-preview")
    preview.add_argument("--manifest", type=Path, required=True)
    preview.add_argument("--current-database", type=Path)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--confirm", action="store_true")
    restore.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            release = _release_from_inputs(args.release, args.version_file)
            payload = create_backup(
                database=args.database,
                output_dir=args.output_dir,
                release=release,
                retention_count=args.retention,
            )
            print("SYSTEMD_BACKUP_RESULT=ok")
            print(f"SYSTEMD_BACKUP_ID={payload['backup_id']}")
            return 0
        if args.command == "restore-preview":
            result = inspect_backup(
                manifest=args.manifest, current_database=args.current_database
            )
            print("SYSTEMD_BACKUP_PREVIEW_RESULT=" + str(result["status"]))
            print("SYSTEMD_BACKUP_INTEGRITY=" + str(result["integrity_ok"]).lower())
            return 0 if result["status"] == "ready" else 1
        result = restore_backup(
            manifest=args.manifest,
            database=args.database,
            confirmed=args.confirm,
            service_stopped=args.service_stopped,
        )
        print("SYSTEMD_BACKUP_RESTORE_RESULT=" + str(result["status"]))
        return 0
    except SystemdBackupError as exc:
        print("SYSTEMD_BACKUP_RESULT=failed")
        print(f"SYSTEMD_BACKUP_CODE={exc.code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
