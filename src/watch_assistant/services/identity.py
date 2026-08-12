"""Shared identifier validation helpers.

Watch-Assistant validates directory/file/plan identifiers across several
modules.  These checks used to be copy-pasted (and drifted) in four files;
this module is the single source of truth.  ``safe_identity`` is the strict
form: ASCII-only, no whitespace, no path/URL separators.
"""

from __future__ import annotations

_IDENTITY_FORBIDDEN_MARKERS = ("/", "\\", "\x00", "://")


def safe_identity(value: object, *, max_length: int = 128) -> bool:
    """Strict identifier check shared by plan/inventory/isolation code.

    Returns True only for a non-empty ASCII string, at most ``max_length``
    characters, containing no whitespace and none of the path/URL markers
    that would allow traversal or confusion with a network URL.
    """
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_length
        and value.isascii()
        and not any(character.isspace() for character in value)
        and not any(marker in value for marker in _IDENTITY_FORBIDDEN_MARKERS)
    )


__all__ = ["safe_identity"]
