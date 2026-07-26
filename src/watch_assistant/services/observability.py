"""Small protocol and failure-safe bridge to the existing LogStore."""

from __future__ import annotations

from typing import Protocol

from watch_assistant.schemas import LoggingLevel


class EventLogger(Protocol):
    async def log_event(
        self,
        event: str,
        *,
        level: LoggingLevel = LoggingLevel.INFO,
        fields: dict[str, object] | None = None,
    ) -> None: ...


async def emit_event(
    event_logger: EventLogger | None,
    event: str,
    *,
    level: LoggingLevel = LoggingLevel.INFO,
    fields: dict[str, object] | None = None,
) -> None:
    if event_logger is None:
        return
    try:
        await event_logger.log_event(event, level=level, fields=fields)
    except Exception:  # noqa: BLE001 - logs must never affect business work
        return
