"""Safe release metadata parsing shared by runtime and release tooling."""

from __future__ import annotations

import re
from pathlib import Path

RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{7}|[0-9a-f]{40}", re.IGNORECASE)
VERSION_COMMIT_PATTERN = re.compile(
    r"^commit=(?P<commit>[0-9a-f]{7}|[0-9a-f]{40})$", re.IGNORECASE | re.MULTILINE
)


def normalize_release(value: str | None) -> str | None:
    """Return a safe, normalized release identifier or ``None``."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if RELEASE_SHA_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate.lower()


def read_release_commit(version_path: Path) -> str | None:
    """Read the exact commit identifier from a release ``VERSION`` file."""

    try:
        version_text = version_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = VERSION_COMMIT_PATTERN.search(version_text)
    return normalize_release(match.group("commit")) if match else None
