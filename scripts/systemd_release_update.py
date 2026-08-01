#!/usr/bin/env python3
"""Derive systemd release metadata from VERSION with an atomic update."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
if SRC_ROOT.is_dir():
    import sys

    sys.path.insert(0, str(SRC_ROOT))

from watch_assistant.release_metadata import read_release_commit


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
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def update_release_metadata(
    *, version_file: Path, release_env: Path, stale_drop_in: Path | None
) -> str:
    release = read_release_commit(version_file)
    if release is None:
        raise ValueError("invalid_release_version")
    _write_atomic(release_env, f"WATCH_ASSISTANT_RELEASE={release}\n")
    if stale_drop_in is not None and stale_drop_in.is_file():
        stale_drop_in.unlink()
    return release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--release-env", type=Path, required=True)
    parser.add_argument("--stale-drop-in", type=Path, required=True)
    args = parser.parse_args()
    try:
        release = update_release_metadata(
            version_file=args.version_file,
            release_env=args.release_env,
            stale_drop_in=args.stale_drop_in,
        )
    except (OSError, ValueError):
        print("SYSTEMD_RELEASE_UPDATE_RESULT=failed")
        print("SYSTEMD_RELEASE_UPDATE_CODE=invalid_release_metadata")
        return 1
    print("SYSTEMD_RELEASE_UPDATE_RESULT=ok")
    print(f"SYSTEMD_RELEASE_UPDATE_RELEASE={release[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
