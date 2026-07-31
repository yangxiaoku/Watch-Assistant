"""Short-lived, server-side 115 QR login sessions."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any

import qrcode

from watch_assistant.adapters.p115_c03_ipad_login import parse_qrcode_token_response
from watch_assistant.services.p115_credentials import normalize_cookie_text
from watch_assistant.services.p115_device_types import P115_DEVICE_CODE_SET

logger = logging.getLogger(__name__)


class P115QrcodeError(ValueError):
    pass


@dataclass
class _Session:
    token: dict[str, Any]
    qr_image: str
    created_at: datetime
    expires_at: datetime
    device_code: str
    device_name: str
    status: str = "waiting"
    cookie: str | None = None
    completed: bool = False


class P115QrcodeService:
    """Keep provider authorization fields in memory and never return them."""

    def __init__(self, *, ttl_seconds: int = 600, timeout_seconds: float = 35):
        self._ttl = timedelta(seconds=ttl_seconds)
        self._timeout = timeout_seconds
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, device_code: str, device_name: str = "扫码设备") -> dict[str, object]:
        device_code = device_code.strip().lower()
        if device_code not in P115_DEVICE_CODE_SET:
            raise P115QrcodeError("invalid_device_code")
        device_name = device_name.strip()
        if not 1 <= len(device_name) <= 64:
            raise P115QrcodeError("invalid_device_name")
        response = await self._call_provider("token", device_code)
        parsed = parse_qrcode_token_response(response)
        if parsed is None:
            raise P115QrcodeError("qrcode_unavailable")
        now = datetime.now(UTC)
        from qrcode.image.svg import SvgPathImage

        image = qrcode.make(parsed.qr_payload(), image_factory=SvgPathImage)
        svg = image.to_string()
        session_id = token_urlsafe(24)
        session = _Session(
            token=parsed.scan_payload(),
            qr_image="data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii"),
            created_at=now,
            expires_at=now + self._ttl,
            device_code=device_code,
            device_name=device_name,
        )
        async with self._lock:
            self._purge(now)
            self._sessions[session_id] = session
        return {
            "session_id": session_id,
            "image_data_url": session.qr_image,
            "expires_at": session.expires_at,
            "status": session.status,
        }

    async def poll(self, session_id: str) -> tuple[str, str | None]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise P115QrcodeError("qrcode_session_not_found")
            now = datetime.now(UTC)
            if now >= session.expires_at:
                session.status = "expired"
                session.cookie = None
                return session.status, None
            if session.cookie is not None:
                return "ready", session.cookie
            if session.completed:
                return "ready", None
            token = dict(session.token)
            device_code = session.device_code
        response = await self._call_provider("status", token)
        status_value = _status_value(response)
        logger.info(
            "115 二维码状态响应 device=%s state=%s code=%s status=%s",
            device_code,
            _response_value(response, "state"),
            _response_value(response, "code"),
            status_value,
        )
        if status_value == 0 and not _result_first_device(device_code):
            return "waiting", None
        if status_value == 1 and not _result_first_device(device_code):
            async with self._lock:
                current = self._sessions.get(session_id)
                if current is not None:
                    current.status = "scanned"
            return "scanned", None
        if status_value in {-1, -2}:
            return "expired", None
        if status_value is None and not _result_first_device(device_code):
            raise P115QrcodeError("qrcode_provider_unavailable")
        if status_value not in {0, 1, 2, None}:
            raise P115QrcodeError("qrcode_provider_unavailable")
        response = await self._call_provider("result", (token["uid"], device_code))
        logger.info(
            "115 二维码登录结果响应 device=%s state=%s code=%s cookie=%s",
            device_code,
            _response_value(response, "state"),
            _response_value(response, "code"),
            _cookie_from_response(response) is not None,
        )
        if _result_pending(response):
            async with self._lock:
                current = self._sessions.get(session_id)
                if current is not None:
                    current.status = "waiting"
            return "waiting", None
        cookie = _cookie_from_response(response)
        if cookie is None:
            raise P115QrcodeError("qrcode_result_invalid")
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise P115QrcodeError("qrcode_session_not_found")
            current.status = "ready"
            current.cookie = cookie
        return "ready", cookie

    async def device_info(self, session_id: str) -> tuple[str, str]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise P115QrcodeError("qrcode_session_not_found")
            return session.device_code, session.device_name

    async def consume(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.cookie = None
                session.status = "ready"
                session.completed = True

    async def _call_provider(self, action: str, value: object) -> object:
        def call() -> object:
            from p115client import P115Client

            if action == "token":
                return P115Client.login_qrcode_token(app=value)
            if action == "status":
                return P115Client.login_qrcode_scan_status(value)  # type: ignore[arg-type]
            uid, device_code = value  # type: ignore[misc]
            return P115Client.login_qrcode_scan_result(uid, app=device_code)

        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider details stay private
            del exc
            raise P115QrcodeError("qrcode_provider_unavailable") from None

    def _purge(self, now: datetime) -> None:
        cutoff = now - self._ttl
        for session_id, session in tuple(self._sessions.items()):
            if session.created_at < cutoff:
                del self._sessions[session_id]


def _status_value(response: object) -> int | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("status")
    if type(value) is int:
        return value
    return None


def _result_first_device(device_code: str) -> bool:
    return device_code in {"ipad", "115ipad", "qipad"}


def _result_pending(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("state") != 0:
        return False
    return response.get("code") in {40101017, "40101017"}


def _response_value(response: object, name: str) -> int | str | None:
    if not isinstance(response, dict):
        return None
    value = response.get(name)
    if type(value) is int:
        return value
    if isinstance(value, str) and 0 < len(value) <= 32 and "\n" not in value and "\r" not in value:
        return value
    return None


def _cookie_from_response(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get("cookie")
    if not isinstance(raw, dict):
        return None
    values = []
    for name in ("UID", "CID", "KID", "SEID"):
        value = raw.get(name)
        if not isinstance(value, str) or not value:
            return None
        values.append(f"{name}={value}")
    return normalize_cookie_text("; ".join(values))


__all__ = ["P115QrcodeError", "P115QrcodeService"]
