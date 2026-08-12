"""Shared lexical path helpers for STRM output handling.

``_absolute_path``/``_same_path`` were copy-pasted identically in
strm_manifest, strm_cleanup_plan and strm_verification; this module is the
single source of truth.
"""

from __future__ import annotations

import os
from pathlib import Path


def absolute_path(value: Path) -> Path:
    """Make a lexical absolute path without following symlinks."""

    return Path(os.path.abspath(os.fspath(value)))


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def valid_id(value: object) -> bool:
    """Strict STRM/cleanup identifier check: ASCII, 1..128, no path separators."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and "/" not in value
        and "\\" not in value
    )


def has_symlink_component(path: Path) -> bool:
    """True if any lexical path component is a symlink."""
    current = Path(path.anchor) if path.anchor else Path.cwd()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            return True
    return False


__all__ = ["absolute_path", "has_symlink_component", "same_path", "valid_id"]
