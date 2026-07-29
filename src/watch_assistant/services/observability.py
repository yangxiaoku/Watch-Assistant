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
        request_id: str | None = None,
        correlation_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        task_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> None: ...


async def emit_event(
    event_logger: EventLogger | None,
    event: str,
    *,
    level: LoggingLevel = LoggingLevel.INFO,
    fields: dict[str, object] | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    task_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> None:
    if event_logger is None:
        return
    try:
        await event_logger.log_event(
            event,
            level=level,
            fields=fields,
            request_id=request_id,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            task_id=task_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except Exception:  # noqa: BLE001 - logs must never affect business work
        return
