"""Read-only, validated credentials for the 115 adapter."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Callable
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
    if not raw or b"\x00" in raw:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return normalize_cookie_text(text)


def normalize_cookie_text(text: str) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    text = text.strip()
    if not text or "\x00" in text:
        return None
    if "\n" in text or "\r" in text:
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
    """Prefer an in-memory managed cookie, then use the configured P115 file.

    连续认证失败超过阈值后临时降级到文件 cookie,降级窗口结束后
    自动重试 managed cookie,避免失效的 managed cookie 让所有
    读接口持续失败且无回退。
    """

    __slots__ = (
        "_clock",
        "_degrade_seconds",
        "_degraded_until",
        "_failure_count",
        "_failure_threshold",
        "_fallback",
        "_managed",
    )

    def __init__(
        self,
        fallback: CookieProvider,
        *,
        failure_threshold: int = 3,
        degrade_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("invalid_failure_threshold")
        if degrade_seconds < 0:
            raise ValueError("invalid_degrade_seconds")
        self._fallback = fallback
        self._managed: str | None = None
        self._failure_count = 0
        self._degraded_until: float | None = None
        self._failure_threshold = int(failure_threshold)
        self._degrade_seconds = float(degrade_seconds)
        self._clock = clock

    def __repr__(self) -> str:
        return "<CompositeCookieProvider>"

    @property
    def fallback(self) -> CookieProvider:
        return self._fallback

    @property
    def source(self) -> str:
        if self._managed is None:
            return "file"
        if self._degraded():
            return "file(degraded)"
        return "managed"

    def set_managed(self, cookie: str | None) -> None:
        self._managed = normalize_cookie_text(cookie) if cookie else None
        if self._managed is not None:
            # 新凭证通常意味着用户重新登录,重置失败计数与降级状态。
            self._failure_count = 0
            self._degraded_until = None

    def notify_failure(self) -> None:
        """Record an authentication failure; degrade to fallback at threshold."""
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._degraded_until = self._clock() + self._degrade_seconds

    def retry_managed(self) -> None:
        """Reset degradation and prefer the managed cookie again immediately."""
        self._degraded_until = None
        self._failure_count = 0

    def _degraded(self) -> bool:
        if self._degraded_until is None:
            return False
        if self._clock() >= self._degraded_until:
            # 降级窗口结束:重置后重新优先 managed。
            self._degraded_until = None
            self._failure_count = 0
            return False
        return True

    def load(self) -> str | None:
        # 注意:成功读取不清零失败计数——managed cookie 失效时每次
        # load 都"成功"返回同一串,清零会让降级永远无法触发。
        if self._managed is not None and not self._degraded():
            return self._managed
        return self._fallback.load()

    read = load
