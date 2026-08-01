#!/usr/bin/env python3
"""Check systemd, VERSION, and health release identities without raw output."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))

from watch_assistant.release_metadata import (
    normalize_release,
    read_release_commit,
)

_EFFECTIVE_RELEASE = re.compile(
    r"(?:^|\s)WATCH_ASSISTANT_RELEASE=([0-9a-f]{7}|[0-9a-f]{40})(?:\s|$)",
    re.IGNORECASE,
)


def _effective_release(systemctl_output: str) -> str | None:
    match = _EFFECTIVE_RELEASE.search(systemctl_output.strip())
    return normalize_release(match.group(1)) if match else None


def _read_health(url: str, timeout: float) -> str | None:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    return normalize_release(payload.get("release")) if isinstance(payload, dict) else None


def check_release_consistency(
    *,
    version_file: Path,
    unit: str,
    health_url: str,
    systemctl_bin: str = "systemctl",
    stale_drop_in: Path | None = None,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    expected = read_release_commit(version_file)
    if expected is None:
        return False, "version_invalid"
    if stale_drop_in is not None and stale_drop_in.exists():
        return False, "stale_release_drop_in"
    try:
        result = subprocess.run(
            [systemctl_bin, "show", unit, "--property=Environment", "--value", "--no-pager"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "systemctl_unavailable"
    if result.returncode != 0:
        return False, "systemctl_show_failed"
    effective = _effective_release(result.stdout)
    health = _read_health(health_url, timeout)
    if effective is None or health is None:
        return False, "release_not_reported"
    if not (expected == effective == health):
        return False, "release_mismatch"
    return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--unit", default="watch-assistant.service")
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--systemctl-bin", default="systemctl")
    parser.add_argument("--stale-drop-in", type=Path)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    passed, code = check_release_consistency(
        version_file=args.version_file,
        unit=args.unit,
        health_url=args.health_url,
        systemctl_bin=args.systemctl_bin,
        stale_drop_in=args.stale_drop_in,
        timeout=args.timeout,
    )
    if not passed:
        print("POSTDEPLOY_RELEASE_CHECK=failed")
        print(f"POSTDEPLOY_RELEASE_CODE={code}")
        return 1
    release = read_release_commit(args.version_file)
    print("POSTDEPLOY_RELEASE_CHECK=ok")
    print(f"POSTDEPLOY_RELEASE={release[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
