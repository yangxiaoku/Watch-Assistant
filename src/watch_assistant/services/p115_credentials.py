"""Read-only, validated credentials for the 115 adapter."""

from __future__ import annotations

import hashlib
import os
import stat
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

MAX_COOKIE_BYTES = 16 * 1024
REQUIRED_COOKIE_NAMES = ("UID", "CID", "KID", "SEID")


class CookieProvider:
    """Load one 115 cookie file without ever writing it or exposing its value."""

    __slots__ = ("_cookie", "_fingerprint", "_path", "_signature")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._signature: tuple[int, int, int, int, int] | None = None
        self._cookie: str | None = None
        self._fingerprint: bytes | None = None

    def __repr__(self) -> str:
        return "<CookieProvider>"

    def load(self) -> str | None:
        signature = self._file_signature()
        if signature == self._signature:
            return self._cookie
        self._signature = signature
        self._cookie = None
        self._fingerprint = None
        if signature is None:
            return None
        cookie = self._read_valid_cookie(signature)
        if cookie is None:
            return None
        self._cookie = cookie
        self._fingerprint = hashlib.sha256(cookie.encode("ascii")).digest()
        return cookie

    read = load

    def _file_signature(self) -> tuple[int, int, int, int, int] | None:
        try:
            file_stat = self._path.lstat()
        except OSError:
            return None
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_mode,
        )

    def _read_valid_cookie(
        self, signature: tuple[int, int, int, int, int]
    ) -> str | None:
        mode = signature[-1]
        if not stat.S_ISREG(mode):
            return None
        if os.name != "nt" and (mode & 0o077):
            return None
        flags = os.O_RDONLY
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(self._path, flags)
            with os.fdopen(file_descriptor, "rb") as cookie_file:
                raw = cookie_file.read(MAX_COOKIE_BYTES + 1)
                opened_stat = os.fstat(cookie_file.fileno())
        except OSError:
            return None
        if not stat.S_ISREG(opened_stat.st_mode):
            return None
        if os.name != "nt" and (opened_stat.st_mode & 0o077):
            return None
        if len(raw) > MAX_COOKIE_BYTES:
            return None
        return _parse_cookie(raw)


def _parse_cookie(raw: bytes) -> str | None:
    if not raw or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return normalize_cookie_text(text)


def normalize_cookie_text(text: str) -> str | None:
    if not isinstance(text, str) or not text or "\x00" in text:
        return None
    if "\n" in text or "\r" in text:
        return None
    text = text.strip()
    if not text:
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(text)
    except CookieError:
        return None
    values: list[str] = []
    for name in REQUIRED_COOKIE_NAMES:
        morsel = parsed.get(name)
        if morsel is None or not morsel.value or morsel.value.strip() != morsel.value:
            return None
        if any(ord(char) < 0x21 or char in ";," for char in morsel.value):
            return None
        values.append(f"{name}={morsel.value}")
    return "; ".join(values)


class CompositeCookieProvider:
    """Prefer an in-memory managed cookie, then use the TgtoDrive file."""

    __slots__ = ("_fallback", "_managed")

    def __init__(self, fallback: CookieProvider) -> None:
        self._fallback = fallback
        self._managed: str | None = None

    def __repr__(self) -> str:
        return "<CompositeCookieProvider>"

    @property
    def fallback(self) -> CookieProvider:
        return self._fallback

    def set_managed(self, cookie: str | None) -> None:
        self._managed = normalize_cookie_text(cookie) if cookie else None

    def load(self) -> str | None:
        if self._managed is not None:
            return self._managed
        return self._fallback.load()

    read = load
