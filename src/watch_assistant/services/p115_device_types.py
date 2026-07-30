"""Verified 115 QR-login device codes supported by the pinned client."""

from __future__ import annotations

from typing import Final, Literal

P115DeviceCode = Literal[
    "web",
    "ios",
    "115ios",
    "android",
    "115android",
    "ipad",
    "115ipad",
    "tv",
    "apple_tv",
    "qandroid",
    "qios",
    "qipad",
    "os_windows",
    "os_mac",
    "os_linux",
    "wechatmini",
    "alipaymini",
    "harmony",
]

P115_DEVICE_CODES: Final[tuple[str, ...]] = (
    "web",
    "ios",
    "115ios",
    "android",
    "115android",
    "ipad",
    "115ipad",
    "tv",
    "apple_tv",
    "qandroid",
    "qios",
    "qipad",
    "os_windows",
    "os_mac",
    "os_linux",
    "wechatmini",
    "alipaymini",
    "harmony",
)

P115_DEVICE_CODE_SET: Final[frozenset[str]] = frozenset(P115_DEVICE_CODES)
