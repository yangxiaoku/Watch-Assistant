"""Translate the repository lock records into pip constraints without uv."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_~-]*$")
_PROJECT_NAME = "watch-assistant"


def build_constraints(lock_file: Path) -> list[str]:
    document = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    packages = document.get("package")
    if not isinstance(packages, list):
        raise TypeError("lock file has no package records")

    versions: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise TypeError("lock file contains an invalid package record")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not _PACKAGE_NAME.fullmatch(name):
            raise ValueError("lock file contains an invalid package name")
        if not isinstance(version, str) or not _PACKAGE_VERSION.fullmatch(version):
            raise ValueError(f"lock file contains an invalid version for {name}")
        if name == _PROJECT_NAME:
            continue
        previous = versions.setdefault(name, version)
        if previous != version:
            raise ValueError(f"lock file has conflicting versions for {name}")

    return [f"{name}=={versions[name]}" for name in sorted(versions)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    constraints = build_constraints(args.lock_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(constraints) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
