"""Defense-in-depth redaction for the standard logging channel.

业务日志(LogStore)在落盘前会经 settings.redact_log_message 脱敏,但
走标准 logging 的模块(systemd 部署下进 journald)不受该通道保护。
本模块提供 RedactingFilter:按敏感模式对日志消息整体脱敏,挂在相关
模块 logger 上后,即使未来某次调用意外带上真实 cookie/token/key 也不会
明文进入 journald。脱敏是结构性的兜底,不替代"日志里根本不写凭据"。
"""

from __future__ import annotations

import logging
import re

_COOKIE_FIELD = re.compile(r"\b(?:UID|CID|KID|SEID|SESSID|PUID)=[^;\s]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:cookie|token|password|passwd|secret|cid|infohash|hash|pickcode|"
    r"share[_ -]?code|extract[_ -]?code|receive[_ -]?code|access[_ -]?code)\b"
    r"\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"\b(?:cookie|token|password|passwd|secret|pickcode)\s+[^\s,;]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_MAGNET = re.compile(r"magnet:\?[^\s]+", re.IGNORECASE)
_INFOHASH = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
_SHARE_URL = re.compile(
    r"https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/(?:s|share)/[^\s\"'<>]+",
    re.IGNORECASE,
)
_URL_QUERY = re.compile(r"([?&][A-Za-z0-9_.-]+=)[^&#\s]+")


def _redact_message(message: str) -> str:
    redacted = str(message)
    redacted = _COOKIE_FIELD.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]", redacted
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + "=[REDACTED]",
        redacted,
    )
    redacted = _SECRET_VALUE.sub(
        lambda match: match.group(0).split(None, 1)[0] + " [REDACTED]",
        redacted,
    )
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _MAGNET.sub("[REDACTED_MAGNET]", redacted)
    redacted = _INFOHASH.sub("[REDACTED_INFOHASH]", redacted)
    redacted = _SHARE_URL.sub("[REDACTED_SHARE]", redacted)
    redacted = _URL_QUERY.sub(r"\1[REDACTED]", redacted)
    return redacted


class RedactingFilter(logging.Filter):
    """Rewrite formatted messages before they reach any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed records must never crash logging
            return True
        redacted = _redact_message(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_redacting_filter(logger: logging.Logger) -> None:
    """Attach the filter once; repeated calls are idempotent."""
    for existing in logger.filters:
        if isinstance(existing, RedactingFilter):
            return
    logger.addFilter(RedactingFilter())


__all__ = ["RedactingFilter", "install_redacting_filter"]
