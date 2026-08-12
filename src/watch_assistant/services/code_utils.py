"""Shared helpers for decoding stored JSON collections.

``_decode_codes`` was copy-pasted in notifications and webhooks with a tiny
drift (one tolerated empty input, the other did not); this module keeps the
robust form.
"""

from __future__ import annotations

import json


def decode_string_set(value: str) -> set[str]:
    """Parse a stored JSON list of strings into a set, tolerating empty input."""
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return set()
    return (
        {item for item in decoded if isinstance(item, str)}
        if isinstance(decoded, list)
        else set()
    )


__all__ = ["decode_string_set"]
