"""Fail-closed DTO boundary for the isolated C03 iPad login helper."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_QR_FALLBACK_PREFIX = "https://115.com/scan/dg-"
_MAX_TOKEN_TEXT_BYTES = 4096
_MAX_QR_TEXT_BYTES = 8192


@dataclass(frozen=True, slots=True, repr=False)
class C03QrcodeToken:
    """Reduced login token; its repr never renders authentication fields."""

    uid: str
    timestamp: int
    sign: str
    qrcode: str | None

    def __repr__(self) -> str:
        return (
            "C03QrcodeToken(uid_present=True, sign_present=True, "
            f"qrcode_present={self.qrcode is not None})"
        )

    def scan_payload(self) -> dict[str, str | int]:
        return {"uid": self.uid, "time": self.timestamp, "sign": self.sign}

    def qr_payload(self) -> str:
        return self.qrcode or _QR_FALLBACK_PREFIX + self.uid


def parse_qrcode_token_response(response: object) -> C03QrcodeToken | None:
    """Reduce the fixed-client token response without retaining unknown fields."""

    if not isinstance(response, Mapping) or not _response_success(response):
        return None
    data = response.get("data")
    if not isinstance(data, Mapping):
        return None

    uid = _safe_text(data.get("uid"), max_bytes=_MAX_TOKEN_TEXT_BYTES)
    sign = _safe_text(data.get("sign"), max_bytes=_MAX_TOKEN_TEXT_BYTES)
    timestamp = data.get("time")
    if (
        uid is None
        or sign is None
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
    ):
        return None

    qrcode_value = data.get("qrcode")
    if qrcode_value in (None, ""):
        qrcode = None
    else:
        qrcode = _safe_text(qrcode_value, max_bytes=_MAX_QR_TEXT_BYTES)
        if qrcode is None:
            return None
    return C03QrcodeToken(uid, timestamp, sign, qrcode)


def _response_success(response: Mapping[str, Any]) -> bool:
    for name in ("state", "success"):
        if name in response and not _success_flag(response[name]):
            return False
    for name in ("code", "errno"):
        if name in response and not _zero_code(response[name]):
            return False
    return True


def _success_flag(value: object) -> bool:
    return value is True or (type(value) is int and value == 1)


def _zero_code(value: object) -> bool:
    if value is False:
        return True
    if type(value) is int:
        return value == 0
    return isinstance(value, str) and value.strip() == "0"


def _safe_text(value: object, *, max_bytes: int) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if "\r" in value or "\n" in value:
        return None
    if len(value.encode("utf-8")) > max_bytes:
        return None
    return value


__all__ = ["C03QrcodeToken", "parse_qrcode_token_response"]
