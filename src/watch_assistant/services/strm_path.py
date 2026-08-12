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


__all__ = ["absolute_path", "same_path"]
