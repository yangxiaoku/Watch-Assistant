"""Stable STRM contract error shared across manifest submodules.

Split from strm_manifest (phase D): the error must live below strm_fs and
strm_fencing so neither submodule imports strm_manifest (avoiding a cycle).
"""

from __future__ import annotations


class StrmManifestError(ValueError):
    """Stable local STRM contract error."""


__all__ = ["StrmManifestError"]
