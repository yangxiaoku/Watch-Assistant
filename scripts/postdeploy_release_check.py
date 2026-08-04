#!/usr/bin/env python3
"""Check systemd, VERSION, and health release identities without raw output."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

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
from systemd_unit import SystemdUnitError, verify_installed_unit

from watch_assistant.release_metadata import (
    normalize_full_release,
    normalize_release,
)

_RELEASE_ENV_NAME = "WATCH_ASSISTANT_RELEASE"
_DEFAULT_RELEASE_ENV = Path("/var/lib/watch-assistant/release.env")
_SYSTEMD_PROPERTIES = ("EnvironmentFiles", "MainPID", "WorkingDirectory")
DEFAULT_HEALTH_TIMEOUT = 30.0
DEFAULT_HEALTH_POLL_INTERVAL = 1.0
MAX_HEALTH_TIMEOUT = 120.0
MAX_HEALTH_POLL_INTERVAL = 5.0


def _parse_systemd_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in _SYSTEMD_PROPERTIES:
            properties[name] = value.strip()
    return properties


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _environment_file_paths(value: str) -> tuple[str, ...]:
    paths: list[str] = []
    for token in value.split():
        if token.startswith("EnvironmentFiles="):
            token = token.split("=", 1)[1]
        if token.startswith("("):
            continue
        token = token.removeprefix("-")
        if token.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", token):
            paths.append(_path_key(token))
    return tuple(paths)


def _read_release_env(path: Path) -> tuple[str | None, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "release_env_missing"

    release: str | None = None
    for line in content.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == _RELEASE_ENV_NAME:
            if release is not None:
                return None, "release_env_invalid"
            release = normalize_release(value.strip())
            if release is None:
                return None, "release_env_invalid"
    if release is None:
        return None, "release_env_invalid"
    return release, "ok"


def _read_version_commit(path: Path) -> str | None:
    try:
        return validate_version_commit_file(path)
    except ReleaseManifestError:
        return None


def _read_service_identity(
    properties: dict[str, str], current_root: Path
) -> tuple[bool, str]:
    try:
        main_pid = int(properties.get("MainPID", "0"))
    except ValueError:
        return False, "main_pid_invalid"
    if main_pid <= 0:
        return False, "service_not_running"

    working_directory = properties.get("WorkingDirectory", "").strip()
    if not working_directory or _path_key(working_directory) != _path_key(current_root):
        return False, "service_path_mismatch"
    return True, "ok"


def _read_health(url: str, timeout: float) -> str | None:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return normalize_release(payload.get("release"))


def _validate_health_polling(
    health_timeout: float, health_poll_interval: float
) -> bool:
    return (
        math.isfinite(health_timeout)
        and 0 < health_timeout <= MAX_HEALTH_TIMEOUT
        and math.isfinite(health_poll_interval)
        and 0 < health_poll_interval <= MAX_HEALTH_POLL_INTERVAL
    )


def _wait_for_health(
    url: str,
    request_timeout: float,
    expected_release: str,
    health_timeout: float,
    health_poll_interval: float,
) -> tuple[str | None, bool]:
    """Poll readiness until the expected release is reported or the deadline expires."""

    deadline = time.monotonic() + health_timeout
    max_attempts = max(1, math.ceil(health_timeout / health_poll_interval) + 1)
    saw_health = False
    last_health: str | None = None
    for _attempt in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        health = _read_health(url, min(request_timeout, remaining))
        if health is not None:
            saw_health = True
            last_health = health
            if health == expected_release:
                return health, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(health_poll_interval, remaining))
    return last_health, saw_health


def _read_diagnostics(
    url: str, timeout: float, token: str | None, expected: str | None = None
) -> str:
    if not token:
        return "diagnostics_token_missing"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.getcode() != 200:
                return "diagnostics_http_failed"
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return "diagnostics_unavailable"
    if not isinstance(payload, dict):
        return "diagnostics_invalid"
    release = normalize_release(payload.get("release"))
    if release is None:
        return "diagnostics_release_invalid"
    if expected is not None and release != expected:
        return "diagnostics_release_mismatch"
    if payload.get("database_integrity") != "supported":
        return "database_integrity_not_supported"
    pending = payload.get("pending_migrations")
    if not isinstance(pending, list):
        return "pending_migrations_invalid"
    if pending:
        return "pending_migrations"
    return "ok"


def check_release_consistency(
    *,
    version_file: Path,
    unit: str,
    health_url: str,
    systemctl_bin: str = "systemctl",
    stale_drop_in: Path | None = None,
    release_env: Path = _DEFAULT_RELEASE_ENV,
    current_root: Path | None = None,
    expected_release: str | None = None,
    diagnostics_url: str | None = None,
    diagnostics_token: str | None = None,
    unit_path: Path | None = None,
    drop_in_dir: Path | None = None,
    unit_uid: int = 0,
    unit_gid: int = 0,
    timeout: float = 5.0,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
    health_poll_interval: float = DEFAULT_HEALTH_POLL_INTERVAL,
) -> tuple[bool, str]:
    if not _validate_health_polling(health_timeout, health_poll_interval):
        return False, "health_polling_invalid"
    expected = _read_version_commit(version_file)
    if expected is None:
        return False, "version_invalid"
    if expected_release is not None:
        exact_expected = normalize_full_release(expected_release)
        if exact_expected is None:
            return False, "expected_release_invalid"
        if expected != exact_expected:
            return False, "version_expected_mismatch"
    current_root = current_root or version_file.parent
    current_release = _read_version_commit(current_root / "VERSION")
    if current_release is None:
        return False, "current_version_invalid"
    if current_release != expected:
        return False, "current_release_mismatch"
    if stale_drop_in is not None and stale_drop_in.exists():
        return False, "stale_release_drop_in"
    expected_unit_sha256: str | None = None
    if unit_path is not None:
        try:
            manifest = validate_build_manifest_file(
                current_root / "release-manifest.json",
                expected_commit=expected,
                expected_short_commit=expected[:7]
                if len(expected) == 40
                else None,
                expected_branch="codex/publish-main",
                require_unit_sha256=True,
            )
        except ReleaseManifestError:
            return False, "unit_manifest_invalid"
        value = manifest.get("unit_sha256")
        if not isinstance(value, str):
            return False, "unit_manifest_invalid"
        expected_unit_sha256 = value
        try:
            verify_installed_unit(
                current_root,
                destination=unit_path,
                expected_sha256=expected_unit_sha256,
                unit_uid=unit_uid,
                unit_gid=unit_gid,
                drop_in_dir=drop_in_dir,
            )
        except SystemdUnitError as exc:
            return False, exc.code
    try:
        result = subprocess.run(
            [
                systemctl_bin,
                "show",
                unit,
                *[f"--property={name}" for name in _SYSTEMD_PROPERTIES],
                "--no-pager",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "systemctl_unavailable"
    if result.returncode != 0:
        return False, "systemctl_show_failed"

    properties = _parse_systemd_properties(result.stdout)
    loaded_files = _environment_file_paths(properties.get("EnvironmentFiles", ""))
    if _path_key(release_env) not in loaded_files:
        return False, "release_env_not_loaded"
    configured_release, release_env_code = _read_release_env(release_env)
    if configured_release is None:
        return False, release_env_code
    if configured_release != expected:
        return False, "release_env_mismatch"

    service_ok, service_code = _read_service_identity(properties, current_root)
    if not service_ok:
        return False, service_code

    health, _saw_health = _wait_for_health(
        health_url,
        timeout,
        expected,
        health_timeout,
        health_poll_interval,
    )
    if health is None:
        return False, "health_not_reported"
    if health != expected:
        return False, "release_mismatch"
    if diagnostics_url is not None:
        diagnostics_code = _read_diagnostics(
            diagnostics_url, timeout, diagnostics_token, expected
        )
        if diagnostics_code != "ok":
            return False, diagnostics_code
    return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--unit", default="watch-assistant.service")
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--systemctl-bin", default="systemctl")
    parser.add_argument("--stale-drop-in", type=Path)
    parser.add_argument("--unit-path", type=Path)
    parser.add_argument("--drop-in-dir", type=Path)
    parser.add_argument("--release-env", type=Path, default=_DEFAULT_RELEASE_ENV)
    parser.add_argument("--current-root", type=Path)
    parser.add_argument("--expected-release")
    parser.add_argument("--diagnostics-url")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--health-timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT
    )
    parser.add_argument(
        "--health-poll-interval", type=float, default=DEFAULT_HEALTH_POLL_INTERVAL
    )
    args = parser.parse_args()
    passed, code = check_release_consistency(
        version_file=args.version_file,
        unit=args.unit,
        health_url=args.health_url,
        systemctl_bin=args.systemctl_bin,
        stale_drop_in=args.stale_drop_in,
        release_env=args.release_env,
        current_root=args.current_root,
        expected_release=args.expected_release,
        diagnostics_url=args.diagnostics_url,
        diagnostics_token=os.environ.get("WATCH_ASSISTANT_DIAGNOSTICS_TOKEN"),
        unit_path=args.unit_path,
        drop_in_dir=args.drop_in_dir,
        timeout=args.timeout,
        health_timeout=args.health_timeout,
        health_poll_interval=args.health_poll_interval,
    )
    if not passed:
        print("POSTDEPLOY_RELEASE_CHECK=failed")
        print(f"POSTDEPLOY_RELEASE_CODE={code}")
        return 1
    release = _read_version_commit(args.version_file)
    print("POSTDEPLOY_RELEASE_CHECK=ok")
    print(f"POSTDEPLOY_RELEASE={release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
