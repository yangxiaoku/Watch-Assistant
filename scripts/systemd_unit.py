#!/usr/bin/env python3
"""Validate and atomically manage the Watch Assistant systemd unit."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import NamedTuple

UNIT_NAME = "watch-assistant.service"
DEFAULT_UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
DEFAULT_DROP_IN_DIR = DEFAULT_UNIT_PATH.parent / f"{UNIT_NAME}.d"
UNIT_MODE = 0o644
BACKUP_MODE = 0o600


class SystemdUnitError(ValueError):
    """A stable, non-sensitive systemd unit gate failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class UnitSnapshot(NamedTuple):
    exists: bool
    sha256: str | None
    mode: int | None
    uid: int | None
    gid: int | None


class UnitChange(NamedTuple):
    source: Path
    destination: Path
    source_sha256: str
    destination_before: UnitSnapshot
    install: bool
    backup_path: Path | None
    backup_sha256: str | None
    backup_mode: int | None
    backup_uid: int | None
    backup_gid: int | None


_REQUIRED_LINES = (
    "[Unit]",
    "Description=Watch Assistant",
    "[Service]",
    "Type=simple",
    "User=watch-assistant",
    "Group=watch-assistant",
    "WorkingDirectory=/opt/watch-assistant/current",
    "EnvironmentFile=/etc/watch-assistant.env",
    "EnvironmentFile=-/var/lib/watch-assistant/release.env",
    "Environment=FRONTEND_DIST_DIR=/opt/watch-assistant/current/frontend/dist",
    "Environment=HOME=/var/lib/watch-assistant",
    "ExecStart=/opt/watch-assistant/venv/bin/uvicorn watch_assistant.app:app --host 0.0.0.0 --port 8115 --workers 1",
    "[Install]",
    "WantedBy=multi-user.target",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, UnicodeError) as exc:
        raise SystemdUnitError("unit_unreadable") from exc
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolved_directory(path: Path, code: str) -> Path:
    try:
        if path.is_symlink():
            raise SystemdUnitError(code)
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except SystemdUnitError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SystemdUnitError(code) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemdUnitError(code)
    return resolved


def _validate_destination_scope(destination: Path) -> Path:
    if destination.name != UNIT_NAME or destination.is_symlink():
        raise SystemdUnitError("unit_destination_scope")
    parent = _resolved_directory(destination.parent, "unit_destination_scope")
    if destination.parent.resolve(strict=True) != parent:
        raise SystemdUnitError("unit_destination_scope")
    current = parent
    while current != current.parent:
        try:
            if current.is_symlink():
                raise SystemdUnitError("unit_destination_scope")
            metadata = current.stat()
        except SystemdUnitError:
            raise
        except (OSError, RuntimeError) as exc:
            raise SystemdUnitError("unit_destination_scope") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise SystemdUnitError("unit_destination_scope")
        current = current.parent
    return parent


def _validate_source_scope(source: Path, release_root: Path) -> Path:
    try:
        root = release_root.resolve(strict=True)
        if source.is_symlink():
            raise SystemdUnitError("unit_source_scope")
        resolved = source.resolve(strict=True)
    except SystemdUnitError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SystemdUnitError("unit_source_scope") from exc
    if not _is_within(resolved, root) or resolved.name != UNIT_NAME:
        raise SystemdUnitError("unit_source_scope")
    return resolved


def _validate_regular_file(path: Path, code: str) -> os.stat_result:
    try:
        if path.is_symlink():
            raise SystemdUnitError(code)
        metadata = path.stat()
    except SystemdUnitError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SystemdUnitError(code) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemdUnitError(code)
    return metadata


def _validate_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    uid: int,
    gid: int,
    mode: int,
    code_prefix: str,
) -> None:
    if metadata.st_uid != uid:
        raise SystemdUnitError(f"{code_prefix}_owner")
    if metadata.st_gid != gid:
        raise SystemdUnitError(f"{code_prefix}_group")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise SystemdUnitError(f"{code_prefix}_mode")


def validate_unit_content(path: Path) -> None:
    metadata = _validate_regular_file(path, "unit_source_invalid")
    if stat.S_IMODE(metadata.st_mode) != UNIT_MODE:
        raise SystemdUnitError("unit_source_mode")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemdUnitError("unit_source_content") from exc
    if "\x00" in content or "\r" in content:
        raise SystemdUnitError("unit_source_content")
    lines = {line.strip() for line in content.splitlines() if line.strip()}
    if any(required not in lines for required in _REQUIRED_LINES):
        raise SystemdUnitError("unit_source_content")


def source_path(release_root: Path) -> Path:
    return _validate_source_scope(release_root / "deploy" / UNIT_NAME, release_root)


def validate_package_unit(
    release_root: Path,
    *,
    expected_sha256: str | None = None,
    source_uid: int | None = None,
    source_gid: int | None = None,
) -> tuple[Path, str]:
    source = source_path(release_root)
    metadata = _validate_regular_file(source, "unit_source_invalid")
    uid = os.geteuid() if source_uid is None and hasattr(os, "geteuid") else source_uid
    gid = os.getegid() if source_gid is None and hasattr(os, "getegid") else source_gid
    if uid is not None and gid is not None:
        _validate_metadata(
            source,
            metadata,
            uid=uid,
            gid=gid,
            mode=UNIT_MODE,
            code_prefix="unit_source",
        )
    elif stat.S_IMODE(metadata.st_mode) != UNIT_MODE:
        raise SystemdUnitError("unit_source_mode")
    validate_unit_content(source)
    digest = sha256(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise SystemdUnitError("unit_source_sha256_mismatch")
    return source, digest


def _snapshot(path: Path, *, uid: int, gid: int) -> UnitSnapshot:
    if not path.exists() and not path.is_symlink():
        return UnitSnapshot(False, None, None, None, None)
    metadata = _validate_regular_file(path, "unit_destination_invalid")
    _validate_metadata(
        path,
        metadata,
        uid=uid,
        gid=gid,
        mode=UNIT_MODE,
        code_prefix="unit_destination",
    )
    return UnitSnapshot(
        True,
        sha256(path),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _drop_in_directory(destination: Path, drop_in_dir: Path | None) -> Path:
    return drop_in_dir if drop_in_dir is not None else destination.parent / f"{UNIT_NAME}.d"


def validate_no_drop_ins(
    destination: Path,
    *,
    drop_in_dir: Path | None = None,
) -> None:
    directory = _drop_in_directory(destination, drop_in_dir)
    if not directory.exists() and not directory.is_symlink():
        return
    resolved = _resolved_directory(directory, "unit_drop_in_scope")
    if resolved.parent != destination.parent.resolve(strict=True) or resolved.name != (
        f"{UNIT_NAME}.d"
    ):
        raise SystemdUnitError("unit_drop_in_scope")
    try:
        entries = tuple(resolved.iterdir())
    except OSError as exc:
        raise SystemdUnitError("unit_drop_in_unreadable") from exc
    if entries:
        raise SystemdUnitError("unit_drop_in_present")


def _backup_path(state_directory: Path) -> Path:
    if state_directory.is_symlink():
        raise SystemdUnitError("unit_backup_scope")
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        directory = state_directory.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemdUnitError("unit_backup_scope") from exc
    if not directory.is_dir():
        raise SystemdUnitError("unit_backup_scope")
    return directory / f".watch-assistant-unit.{os.getpid()}.{uuid.uuid4().hex}.bak"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise SystemdUnitError("unit_backup_sync_failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise SystemdUnitError("unit_backup_sync_failed") from exc
    finally:
        os.close(descriptor)


def _atomic_copy(
    source: Path,
    destination: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
    except OSError as exc:
        raise SystemdUnitError("unit_write_failed") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            with source.open("rb") as source_stream:
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        if hasattr(os, "chown"):
            os.chown(temporary, uid, gid)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except (OSError, SystemdUnitError) as exc:
        raise exc if isinstance(exc, SystemdUnitError) else SystemdUnitError(
            "unit_write_failed"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_restore(
    backup: Path,
    destination: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    _atomic_copy(backup, destination, mode=mode, uid=uid, gid=gid)


def plan_unit_change(
    release_root: Path,
    *,
    destination: Path,
    state_directory: Path,
    install: bool,
    expected_source_sha256: str | None = None,
    unit_uid: int = 0,
    unit_gid: int = 0,
    source_uid: int | None = None,
    source_gid: int | None = None,
    drop_in_dir: Path | None = None,
) -> UnitChange:
    _validate_destination_scope(destination)
    validate_no_drop_ins(destination, drop_in_dir=drop_in_dir)
    source, source_digest = validate_package_unit(
        release_root,
        expected_sha256=expected_source_sha256,
        source_uid=source_uid,
        source_gid=source_gid,
    )
    before = _snapshot(destination, uid=unit_uid, gid=unit_gid)
    if before.exists and before.sha256 == source_digest:
        return UnitChange(
            source,
            destination,
            source_digest,
            before,
            False,
            None,
            None,
            None,
            None,
            None,
        )
    if not install:
        if not before.exists:
            raise SystemdUnitError("unit_missing")
        raise SystemdUnitError("unit_drift")
    backup_path: Path | None = None
    backup_sha256: str | None = None
    backup_mode: int | None = None
    backup_uid: int | None = None
    backup_gid: int | None = None
    if before.exists:
        backup_path = _backup_path(state_directory)
        backup_sha256 = before.sha256
        backup_mode = before.mode
        backup_uid = before.uid
        backup_gid = before.gid
    return UnitChange(
        source,
        destination,
        source_digest,
        before,
        True,
        backup_path,
        backup_sha256,
        backup_mode,
        backup_uid,
        backup_gid,
    )


def _validate_change_destination(change: UnitChange, *, unit_uid: int, unit_gid: int) -> None:
    current = _snapshot(change.destination, uid=unit_uid, gid=unit_gid)
    if current != change.destination_before:
        raise SystemdUnitError("unit_destination_changed")


def apply_unit_change(
    change: UnitChange,
    *,
    unit_uid: int = 0,
    unit_gid: int = 0,
) -> None:
    if not change.install:
        return
    _validate_change_destination(change, unit_uid=unit_uid, unit_gid=unit_gid)
    current_source_digest = sha256(change.source)
    if current_source_digest != change.source_sha256:
        raise SystemdUnitError("unit_source_changed")
    validate_unit_content(change.source)
    if change.backup_path is not None:
        _atomic_copy(
            change.destination,
            change.backup_path,
            mode=BACKUP_MODE,
            uid=unit_uid,
            gid=unit_gid,
        )
        if sha256(change.backup_path) != change.backup_sha256:
            raise SystemdUnitError("unit_backup_sha256_mismatch")
    _atomic_copy(
        change.source,
        change.destination,
        mode=UNIT_MODE,
        uid=unit_uid,
        gid=unit_gid,
    )
    installed = _snapshot(change.destination, uid=unit_uid, gid=unit_gid)
    if installed.sha256 != change.source_sha256:
        raise SystemdUnitError("unit_install_sha256_mismatch")


def _validate_backup(change: UnitChange, *, unit_uid: int, unit_gid: int) -> None:
    if change.backup_path is None or change.backup_sha256 is None:
        raise SystemdUnitError("unit_backup_missing")
    backup = _validate_regular_file(change.backup_path, "unit_backup_missing")
    if stat.S_IMODE(backup.st_mode) != BACKUP_MODE:
        raise SystemdUnitError("unit_backup_mode")
    if backup.st_uid != unit_uid or backup.st_gid != unit_gid:
        raise SystemdUnitError("unit_backup_owner")
    if sha256(change.backup_path) != change.backup_sha256:
        raise SystemdUnitError("unit_backup_sha256_mismatch")


def restore_unit_change(
    change: UnitChange,
    *,
    unit_uid: int = 0,
    unit_gid: int = 0,
) -> None:
    if not change.install:
        return
    installed = _snapshot(change.destination, uid=unit_uid, gid=unit_gid)
    if installed.sha256 not in {
        change.source_sha256,
        change.destination_before.sha256,
    }:
        raise SystemdUnitError("unit_changed_after_install")
    if change.destination_before.exists:
        if installed.sha256 == change.destination_before.sha256 and (
            change.backup_path is None or not change.backup_path.exists()
        ):
            return
        _validate_backup(change, unit_uid=unit_uid, unit_gid=unit_gid)
        _atomic_restore(
            change.backup_path,
            change.destination,
            mode=change.backup_mode or UNIT_MODE,
            uid=change.backup_uid if change.backup_uid is not None else unit_uid,
            gid=change.backup_gid if change.backup_gid is not None else unit_gid,
        )
        restored = _snapshot(
            change.destination,
            uid=change.backup_uid if change.backup_uid is not None else unit_uid,
            gid=change.backup_gid if change.backup_gid is not None else unit_gid,
        )
        if restored != change.destination_before:
            raise SystemdUnitError("unit_restore_verification_failed")
        return
    if not installed.exists:
        return
    try:
        change.destination.unlink()
        _fsync_directory(change.destination.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SystemdUnitError("unit_restore_failed") from exc


def verify_installed_unit(
    release_root: Path,
    *,
    destination: Path,
    expected_sha256: str | None = None,
    unit_uid: int = 0,
    unit_gid: int = 0,
    source_uid: int | None = None,
    source_gid: int | None = None,
    drop_in_dir: Path | None = None,
) -> str:
    _validate_destination_scope(destination)
    validate_no_drop_ins(destination, drop_in_dir=drop_in_dir)
    _source, source_digest = validate_package_unit(
        release_root,
        expected_sha256=expected_sha256,
        source_uid=source_uid,
        source_gid=source_gid,
    )
    installed = _snapshot(destination, uid=unit_uid, gid=unit_gid)
    if not installed.exists:
        raise SystemdUnitError("unit_missing")
    if installed.sha256 != source_digest:
        raise SystemdUnitError("unit_drift")
    return source_digest


def verify_installed_unit_digest(
    *,
    destination: Path,
    expected_sha256: str,
    unit_uid: int = 0,
    unit_gid: int = 0,
    drop_in_dir: Path | None = None,
) -> str:
    """Verify an installed unit when its trusted digest is saved externally."""

    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise SystemdUnitError("unit_expected_sha256_invalid")
    _validate_destination_scope(destination)
    validate_no_drop_ins(destination, drop_in_dir=drop_in_dir)
    installed = _snapshot(destination, uid=unit_uid, gid=unit_gid)
    if not installed.exists:
        raise SystemdUnitError("unit_missing")
    # The digest proves identity; the content check keeps this path subject to
    # the same unit contract as package-backed verification.
    validate_unit_content(destination)
    if installed.sha256 != expected_sha256:
        raise SystemdUnitError("unit_drift")
    return installed.sha256
