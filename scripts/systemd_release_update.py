#!/usr/bin/env python3
"""Atomically switch a systemd release and retain a safe rollback checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
SCRIPTS_ROOT = SCRIPT_ROOT / "scripts"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))
if SCRIPTS_ROOT.is_dir():
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_manifest import (
    ReleaseManifestError,
    validate_build_manifest_file,
    validate_version_commit_file,
)
from systemd_unit import (
    SystemdUnitError,
    UnitChange,
    UnitSnapshot,
    apply_unit_change,
    plan_unit_change,
    restore_unit_change,
    verify_installed_unit,
)

from watch_assistant.release_metadata import (
    normalize_full_release,
    normalize_release,
)

_RELEASE_ENV_NAME = "WATCH_ASSISTANT_RELEASE"
_STATE_SCHEMA_VERSION = 2


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update_release_metadata(
    *, version_file: Path, release_env: Path, stale_drop_in: Path | None
) -> str:
    """Keep the legacy metadata-only operation for offline callers."""

    try:
        release = validate_version_commit_file(version_file)
    except ReleaseManifestError:
        raise ValueError("invalid_release_version")
    _write_atomic(release_env, f"{_RELEASE_ENV_NAME}={release}\n")
    # A drop-in may contain administrator-managed values.  The metadata-only
    # compatibility path must never remove it; formal deployment checks it.
    return release


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_release_env(path: Path) -> tuple[str | None, bool]:
    if not path.exists():
        return None, False
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("previous_release_env_unreadable") from exc
    release: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or name != _RELEASE_ENV_NAME or release is not None:
            raise ValueError("previous_release_env_invalid")
        release = normalize_release(value)
        if release is None:
            raise ValueError("previous_release_env_invalid")
    if release is None:
        raise ValueError("previous_release_env_invalid")
    return release, True


def _write_release_env(path: Path, release: str) -> None:
    if normalize_full_release(release) is None:
        raise ValueError("invalid_release_metadata")
    _write_atomic(path, f"{_RELEASE_ENV_NAME}={release}\n")


def _remove_current(current_root: Path) -> None:
    if current_root.exists() or current_root.is_symlink():
        if not current_root.is_symlink():
            raise ValueError("current_root_not_symlink")
        current_root.unlink()
        _fsync_directory(current_root.parent)


def _current_target(current_root: Path) -> Path | None:
    if not current_root.exists() and not current_root.is_symlink():
        return None
    if not current_root.is_symlink():
        raise ValueError("current_root_not_symlink")
    try:
        target = current_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("current_root_target_missing") from exc
    if not target.is_dir():
        raise ValueError("current_root_target_not_directory")
    return target


def _validate_release_root(release_root: Path, allowed_root: Path) -> Path:
    try:
        if release_root.is_symlink():
            raise ValueError("release_root_symlink")
        allowed = allowed_root.resolve(strict=True)
        release = release_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("release_root_unavailable") from exc
    if not release.is_dir() or release == allowed or not _is_within(release, allowed):
        raise ValueError("release_root_out_of_scope")
    return release


def _validate_previous_target(target: str | None, allowed_root: Path) -> Path | None:
    if target is None:
        return None
    try:
        resolved_allowed = allowed_root.resolve(strict=True)
        resolved_target = Path(target).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("previous_current_missing") from exc
    if (
        not resolved_target.is_dir()
        or resolved_target == resolved_allowed
        or not _is_within(resolved_target, resolved_allowed)
    ):
        raise ValueError("previous_current_out_of_scope")
    return resolved_target


def _validate_saved_release(
    release_root: Path,
    expected_release: str | None,
    expected_manifest_sha256: str | None,
    expected_version_sha256: str | None,
    *,
    code_prefix: str,
) -> None:
    if expected_release is not None:
        try:
            actual_release = validate_version_commit_file(release_root / "VERSION")
        except ReleaseManifestError:
            raise ValueError(f"{code_prefix}_version_invalid") from None
        if actual_release != expected_release:
            raise ValueError(f"{code_prefix}_version_mismatch")
    if expected_manifest_sha256 is not None and _manifest_sha256(release_root) != (
        expected_manifest_sha256
    ):
        raise ValueError(f"{code_prefix}_manifest_changed")
    if expected_version_sha256 is not None and _file_sha256(release_root / "VERSION") != (
        expected_version_sha256
    ):
        raise ValueError(f"{code_prefix}_version_changed")


def _atomic_switch(current_root: Path, release_root: Path) -> None:
    if (current_root.exists() or current_root.is_symlink()) and not current_root.is_symlink():
        raise ValueError("current_root_not_symlink")
    current_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = current_root.parent / (
        f".{current_root.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        os.symlink(str(release_root), temporary, target_is_directory=True)
        os.replace(temporary, current_root)
        _fsync_directory(current_root.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _write_state(path: Path, payload: dict[str, object]) -> None:
    _write_atomic(
        path,
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
    )


def _read_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("rollback_state_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _STATE_SCHEMA_VERSION:
        raise ValueError("rollback_state_invalid")
    return payload


def _state_digest(
    state: dict[str, object], key: str, *, required: bool
) -> str | None:
    value = state.get(key)
    if value is None:
        if required:
            raise ValueError("rollback_state_invalid")
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("rollback_state_invalid")
    return value


def _unit_state(change: UnitChange) -> dict[str, object]:
    previous = change.destination_before
    return {
        "unit_managed": True,
        "unit_path": _path_key(change.destination),
        "unit_source": str(change.source),
        "unit_source_sha256": change.source_sha256,
        "unit_install": change.install,
        "unit_previous_exists": previous.exists,
        "unit_previous_sha256": previous.sha256,
        "unit_previous_mode": previous.mode,
        "unit_previous_uid": previous.uid,
        "unit_previous_gid": previous.gid,
        "unit_backup_path": str(change.backup_path) if change.backup_path else None,
        "unit_backup_sha256": change.backup_sha256,
        "unit_backup_mode": change.backup_mode,
        "unit_backup_uid": change.backup_uid,
        "unit_backup_gid": change.backup_gid,
    }


def _state_int(state: dict[str, object], key: str, *, required: bool) -> int | None:
    value = state.get(key)
    if value is None:
        if required:
            raise ValueError("rollback_state_invalid")
        return None
    if type(value) is not int or value < 0:
        raise ValueError("rollback_state_invalid")
    return value


def _unit_change_from_state(
    state: dict[str, object],
    *,
    target: Path,
    unit_path: Path,
    state_directory: Path,
) -> UnitChange | None:
    if state.get("unit_managed") is not True:
        if any(key.startswith("unit_") for key in state):
            raise ValueError("rollback_state_invalid")
        return None
    stored_path = state.get("unit_path")
    if not isinstance(stored_path, str) or stored_path != _path_key(unit_path):
        raise ValueError("rollback_unit_path_mismatch")
    source_digest = _state_digest(state, "unit_source_sha256", required=True)
    previous_exists = state.get("unit_previous_exists")
    if not isinstance(previous_exists, bool):
        raise TypeError("rollback_state_invalid")
    previous_sha256 = _state_digest(
        state, "unit_previous_sha256", required=previous_exists
    )
    previous_mode = _state_int(state, "unit_previous_mode", required=previous_exists)
    previous_uid = _state_int(state, "unit_previous_uid", required=previous_exists)
    previous_gid = _state_int(state, "unit_previous_gid", required=previous_exists)
    install = state.get("unit_install")
    if not isinstance(install, bool):
        raise TypeError("rollback_state_invalid")
    backup_value = state.get("unit_backup_path")
    backup_path = Path(backup_value) if isinstance(backup_value, str) else None
    if backup_path is not None:
        try:
            if backup_path.resolve(strict=False).parent != state_directory.resolve(strict=True):
                raise ValueError("rollback_unit_backup_scope")
        except (OSError, RuntimeError) as exc:
            raise ValueError("rollback_unit_backup_scope") from exc
    backup_sha256 = _state_digest(
        state, "unit_backup_sha256", required=install and previous_exists
    )
    backup_mode = _state_int(state, "unit_backup_mode", required=install and previous_exists)
    backup_uid = _state_int(state, "unit_backup_uid", required=install and previous_exists)
    backup_gid = _state_int(state, "unit_backup_gid", required=install and previous_exists)
    if install and previous_exists and backup_path is None:
        raise ValueError("rollback_state_invalid")
    previous = UnitSnapshot(
        previous_exists,
        previous_sha256,
        previous_mode,
        previous_uid,
        previous_gid,
    )
    return UnitChange(
        target / "deploy" / "watch-assistant.service",
        unit_path,
        source_digest,
        previous,
        install,
        backup_path,
        backup_sha256,
        backup_mode,
        backup_uid,
        backup_gid,
    )


def _manifest_sha256(release_root: Path) -> str | None:
    manifest = release_root / "release-manifest.json"
    if not manifest.is_file():
        return None
    digest = hashlib.sha256()
    with manifest.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_manifest(
    release_root: Path,
    expected_release: str,
    *,
    require_unit_sha256: bool = False,
) -> dict[str, object]:
    try:
        return validate_build_manifest_file(
            release_root / "release-manifest.json",
            expected_commit=expected_release,
            expected_short_commit=expected_release[:7],
            expected_branch="codex/publish-main",
            require_unit_sha256=require_unit_sha256,
        )
    except ReleaseManifestError as exc:
        raise ValueError(f"release_manifest_{exc.code}") from None


def switch_release(
    *,
    release_root: Path,
    expected_release: str,
    current_root: Path,
    release_env: Path,
    state_file: Path,
    allowed_releases_root: Path,
    stale_drop_in: Path | None = None,
    unit_path: Path | None = None,
    install_unit: bool = False,
    drop_in_dir: Path | None = None,
    unit_uid: int = 0,
    unit_gid: int = 0,
) -> dict[str, object]:
    """Switch release metadata and, when requested, the managed systemd unit."""

    expected = normalize_full_release(expected_release)
    if expected is None:
        raise ValueError("expected_release_invalid")
    target = _validate_release_root(release_root, allowed_releases_root)
    try:
        validate_version_commit_file(
            target / "VERSION",
            expected_commit=expected,
            expected_branch="codex/publish-main",
        )
    except ReleaseManifestError as exc:
        if exc.code == "version_commit_mismatch":
            raise ValueError("version_expected_mismatch") from None
        raise ValueError("version_invalid") from None
    manifest = _validate_release_manifest(
        target,
        expected,
        require_unit_sha256=unit_path is not None,
    )
    previous_target = _current_target(current_root)
    if previous_target is None:
        previous_release = None
    else:
        try:
            previous_release = validate_version_commit_file(
                previous_target / "VERSION"
            )
        except ReleaseManifestError:
            raise ValueError("previous_version_invalid") from None
    if previous_target is not None and previous_release is None:
        raise ValueError("previous_version_invalid")
    if previous_target is not None:
        previous_target = _validate_previous_target(
            str(previous_target), allowed_releases_root
        )
    previous_env, previous_env_present = _read_release_env(release_env)
    if previous_env_present and previous_env != previous_release:
        raise ValueError("previous_release_env_mismatch")
    if previous_target is not None and not previous_env_present:
        raise ValueError("previous_release_env_missing")

    unit_change: UnitChange | None = None
    if unit_path is not None:
        unit_sha256 = manifest.get("unit_sha256")
        if not isinstance(unit_sha256, str):
            raise ValueError("release_manifest_unit_sha256_missing")
        try:
            unit_change = plan_unit_change(
                target,
                destination=unit_path,
                state_directory=state_file.parent,
                install=install_unit,
                expected_source_sha256=unit_sha256,
                unit_uid=unit_uid,
                unit_gid=unit_gid,
                drop_in_dir=drop_in_dir,
            )
        except SystemdUnitError as exc:
            raise ValueError(exc.code) from None

    state: dict[str, object] = {
        "schema_version": _STATE_SCHEMA_VERSION,
        "status": "switching",
        "created_at": datetime.now(UTC).isoformat(),
        "current_root": _path_key(current_root),
        "target_root": str(target),
        "target_release": expected,
        "target_manifest_sha256": _manifest_sha256(target),
        "target_version_sha256": _file_sha256(target / "VERSION"),
        "previous_current_target": str(previous_target) if previous_target else None,
        "previous_current": str(previous_target) if previous_target else None,
        "previous_release": previous_release,
        "previous_manifest_sha256": (
            _manifest_sha256(previous_target) if previous_target else None
        ),
        "previous_version_sha256": (
            _file_sha256(previous_target / "VERSION") if previous_target else None
        ),
        "previous_release_env_present": previous_env_present,
        "previous_release_env_release": previous_env,
    }
    if unit_change is not None:
        state.update(_unit_state(unit_change))
    _write_state(state_file, state)
    try:
        if unit_change is not None:
            try:
                apply_unit_change(
                    unit_change,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                )
            except SystemdUnitError as exc:
                raise ValueError(exc.code) from None
        _atomic_switch(current_root, target)
        _write_release_env(release_env, expected)
        state["status"] = "deployed"
        state["deployed_at"] = datetime.now(UTC).isoformat()
        _write_state(state_file, state)
        return state
    except Exception:
        restore_error: Exception | None = None
        if unit_change is not None and unit_change.install:
            try:
                restore_unit_change(
                    unit_change,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                )
            except SystemdUnitError as exc:
                restore_error = ValueError(exc.code)
        try:
            if previous_target is None:
                _remove_current(current_root)
            else:
                _atomic_switch(current_root, previous_target)
            if previous_env_present and previous_env is not None:
                _write_atomic(release_env, f"{_RELEASE_ENV_NAME}={previous_env}\n")
            elif release_env.exists():
                release_env.unlink()
        except OSError as exc:
            restore_error = restore_error or exc
        if restore_error is not None:
            raise restore_error
        raise


def rollback_release(
    *,
    current_root: Path,
    release_env: Path,
    state_file: Path,
    allowed_releases_root: Path,
    unit_path: Path | None = None,
    drop_in_dir: Path | None = None,
    unit_uid: int = 0,
    unit_gid: int = 0,
) -> dict[str, object]:
    """Restore the recorded previous release and managed unit transactionally."""

    state = _read_state(state_file)
    if state.get("current_root") != _path_key(current_root):
        raise ValueError("rollback_current_mismatch")
    target_value = state.get("target_root")
    if not isinstance(target_value, str):
        raise TypeError("rollback_state_invalid")
    target = _validate_release_root(Path(target_value), allowed_releases_root)
    target_release = state.get("target_release")
    if normalize_full_release(target_release) is None:
        raise ValueError("rollback_state_invalid")
    target_manifest_sha256 = _state_digest(
        state, "target_manifest_sha256", required=True
    )
    target_version_sha256 = _state_digest(
        state, "target_version_sha256", required=True
    )
    _validate_saved_release(
        target,
        str(target_release),
        target_manifest_sha256,
        target_version_sha256,
        code_prefix="rollback_target",
    )
    active = _current_target(current_root)
    if active != target:
        raise ValueError("rollback_target_changed")
    previous_target_value = state.get("previous_current_target")
    if previous_target_value is not None and not isinstance(previous_target_value, str):
        raise ValueError("rollback_state_invalid")
    previous_target = _validate_previous_target(
        previous_target_value, allowed_releases_root
    )
    previous_release = state.get("previous_release")
    if previous_target is None and previous_release is not None:
        raise ValueError("rollback_state_invalid")
    if previous_target is not None and not isinstance(previous_release, str):
        raise ValueError("rollback_state_invalid")
    previous_manifest_sha256 = _state_digest(
        state, "previous_manifest_sha256", required=False
    )
    previous_version_sha256 = _state_digest(
        state, "previous_version_sha256", required=previous_target is not None
    )
    if previous_target is not None:
        previous_manifest_exists = (previous_target / "release-manifest.json").is_file()
        if previous_manifest_exists != (previous_manifest_sha256 is not None):
            raise ValueError("rollback_state_invalid")
        _validate_saved_release(
            previous_target,
            previous_release,
            previous_manifest_sha256,
            previous_version_sha256,
            code_prefix="rollback_previous",
        )
    previous_env_present_value = state.get("previous_release_env_present")
    if not isinstance(previous_env_present_value, bool):
        raise TypeError("rollback_state_invalid")
    previous_env_present = previous_env_present_value
    previous_env_release = state.get("previous_release_env_release")
    if previous_env_present and normalize_release(previous_env_release) is None:
        raise ValueError("previous_release_env_invalid")
    if previous_env_present and previous_env_release != previous_release:
        raise ValueError("previous_release_env_mismatch")
    if not previous_env_present and previous_env_release is not None:
        raise ValueError("rollback_state_invalid")

    unit_change: UnitChange | None = None
    if unit_path is not None:
        unit_change = _unit_change_from_state(
            state,
            target=target,
            unit_path=unit_path,
            state_directory=state_file.parent,
        )
        if unit_change is None:
            raise ValueError("rollback_unit_state_missing")
        try:
            verify_installed_unit(
                target,
                destination=unit_path,
                expected_sha256=unit_change.source_sha256,
                unit_uid=unit_uid,
                unit_gid=unit_gid,
                drop_in_dir=drop_in_dir,
            )
        except SystemdUnitError as exc:
            raise ValueError(exc.code) from None

    try:
        if previous_target is None:
            _remove_current(current_root)
        else:
            _atomic_switch(current_root, previous_target)
        if previous_env_present and isinstance(previous_env_release, str):
            # Preserve a legacy short SHA while restoring the exact saved env value.
            _write_atomic(
                release_env, f"{_RELEASE_ENV_NAME}={previous_env_release}\n"
            )
        elif release_env.exists():
            release_env.unlink()
        if unit_change is not None and unit_change.install:
            try:
                restore_unit_change(
                    unit_change,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                )
            except SystemdUnitError as exc:
                raise ValueError(exc.code) from None
        if unit_path is not None and previous_target is not None:
            try:
                previous_manifest = _validate_release_manifest(
                    previous_target,
                    str(previous_release),
                    require_unit_sha256=True,
                )
                previous_unit_sha256 = previous_manifest.get("unit_sha256")
                if not isinstance(previous_unit_sha256, str):
                    raise TypeError("rollback_previous_unit_manifest_invalid")
                verify_installed_unit(
                    previous_target,
                    destination=unit_path,
                    expected_sha256=previous_unit_sha256,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                    drop_in_dir=drop_in_dir,
                )
            except SystemdUnitError as exc:
                raise ValueError(exc.code) from None
        state["status"] = "rolled_back"
        state["rolled_back_at"] = datetime.now(UTC).isoformat()
        _write_state(state_file, state)
    except Exception:
        if unit_change is not None and unit_change.install:
            try:
                # Reinstall the verified target unit if rollback failed after
                # changing it; the release switch itself is restored below.
                target_unit = plan_unit_change(
                    target,
                    destination=unit_path,
                    state_directory=state_file.parent,
                    install=True,
                    expected_source_sha256=unit_change.source_sha256,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                    drop_in_dir=drop_in_dir,
                )
                apply_unit_change(
                    target_unit,
                    unit_uid=unit_uid,
                    unit_gid=unit_gid,
                )
            except (SystemdUnitError, TypeError):
                pass
        try:
            _atomic_switch(current_root, target)
            target_release = state.get("target_release")
            if normalize_full_release(target_release) is not None:
                _write_release_env(release_env, str(target_release))
        except OSError:
            pass
        raise
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--version-file", type=Path)
    parser.add_argument("--release-env", type=Path, required=True)
    parser.add_argument("--stale-drop-in", type=Path)
    parser.add_argument("--unit-path", type=Path)
    parser.add_argument("--drop-in-dir", type=Path)
    parser.add_argument("--install-unit", action="store_true")
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--expected-release")
    parser.add_argument("--current-root", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--allowed-releases-root", type=Path)
    args = parser.parse_args()
    try:
        if args.switch:
            if not all(
                value is not None
                for value in (
                    args.release_root,
                    args.expected_release,
                    args.current_root,
                    args.state_file,
                    args.allowed_releases_root,
                )
            ):
                raise ValueError("switch_arguments_missing")
            state = switch_release(
                release_root=args.release_root,
                expected_release=args.expected_release,
                current_root=args.current_root,
                release_env=args.release_env,
                state_file=args.state_file,
                allowed_releases_root=args.allowed_releases_root,
                stale_drop_in=args.stale_drop_in,
                unit_path=args.unit_path,
                install_unit=args.install_unit,
                drop_in_dir=args.drop_in_dir,
            )
            print("SYSTEMD_RELEASE_UPDATE_RESULT=ok")
            print(f"SYSTEMD_RELEASE_UPDATE_RELEASE={state['target_release']}")
            return 0
        if args.rollback:
            if not all(
                value is not None
                for value in (
                    args.current_root,
                    args.state_file,
                    args.allowed_releases_root,
                )
            ):
                raise ValueError("rollback_arguments_missing")
            state = rollback_release(
                current_root=args.current_root,
                release_env=args.release_env,
                state_file=args.state_file,
                allowed_releases_root=args.allowed_releases_root,
                unit_path=args.unit_path,
                drop_in_dir=args.drop_in_dir,
            )
            print("SYSTEMD_RELEASE_ROLLBACK_RESULT=ok")
            print(f"SYSTEMD_RELEASE_ROLLBACK_RELEASE={state.get('previous_release')}")
            return 0
        if args.version_file is None:
            raise ValueError("version_file_missing")
        release = update_release_metadata(
            version_file=args.version_file,
            release_env=args.release_env,
            stale_drop_in=args.stale_drop_in,
        )
    except (OSError, TypeError, ValueError) as exc:
        print("SYSTEMD_RELEASE_UPDATE_RESULT=failed")
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = str(exc)
        if re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "invalid_release_metadata"
        print(f"SYSTEMD_RELEASE_UPDATE_CODE={code}")
        return 1
    print("SYSTEMD_RELEASE_UPDATE_RESULT=ok")
    print(f"SYSTEMD_RELEASE_UPDATE_RELEASE={release[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
