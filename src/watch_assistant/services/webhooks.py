"""Reliable, outbound-only Webhook delivery.

The service intentionally accepts only registered business events and stores the
encrypted endpoint secret. It never exposes an inbound command surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import WebhookDelivery, WebhookEndpoint
from watch_assistant.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookEndpointCreateRequest,
    WebhookEndpointCreateResponse,
    WebhookEndpointListResponse,
    WebhookEndpointPatch,
    WebhookEndpointResponse,
)
from watch_assistant.services.event_catalog import get_event_definition
from watch_assistant.services.observability import EventLogger

MAX_ATTEMPTS = 8
DELIVERY_TIMEOUT = 10.0
RETRY_DELAYS = (5, 15, 60, 300, 900, 1800, 3600, 7200)


class WebhookError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WebhookService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        *,
        event_logger: EventLogger | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._event_logger = event_logger
        self._http = http_client or httpx.AsyncClient(follow_redirects=False)
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def create(
        self, request: WebhookEndpointCreateRequest
    ) -> WebhookEndpointCreateResponse:
        url = _validate_url(request.url)
        event_codes = _validate_event_codes(request.event_codes)
        secret = "whsec_" + secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        item = WebhookEndpoint(
            id="webhook_" + uuid4().hex,
            name=request.name.strip(),
            url=url,
            secret_encrypted=self._crypto.encrypt(secret),
            secret_prefix=secret[:13],
            event_codes_json=json.dumps(event_codes, separators=(",", ":")),
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(item)
            await session.commit()
        return WebhookEndpointCreateResponse(secret=secret, item=_endpoint_response(item))

    async def list(self) -> WebhookEndpointListResponse:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(
                    select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc())
                )
            )
        return WebhookEndpointListResponse(items=[_endpoint_response(item) for item in items])

    async def patch(self, endpoint_id: str, patch: WebhookEndpointPatch) -> WebhookEndpointResponse:
        async with self._session_factory() as session:
            item = await session.get(WebhookEndpoint, endpoint_id)
            if item is None:
                raise WebhookError("webhook_not_found")
            if item.revision != patch.revision:
                raise WebhookError("webhook_conflict")
            values = patch.model_dump(exclude_none=True)
            values.pop("revision", None)
            if "url" in values:
                values["url"] = _validate_url(values["url"])
            if "event_codes" in values:
                values["event_codes"] = _validate_event_codes(values["event_codes"])
                item.event_codes_json = json.dumps(values.pop("event_codes"), separators=(",", ":"))
            for key, value in values.items():
                setattr(item, key, value.strip() if key == "name" else value)
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
            return _endpoint_response(item)

    async def rotate(self, endpoint_id: str) -> WebhookEndpointCreateResponse:
        secret = "whsec_" + secrets.token_urlsafe(32)
        async with self._session_factory() as session:
            item = await session.get(WebhookEndpoint, endpoint_id)
            if item is None:
                raise WebhookError("webhook_not_found")
            item.secret_encrypted = self._crypto.encrypt(secret)
            item.secret_prefix = secret[:13]
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
            return WebhookEndpointCreateResponse(secret=secret, item=_endpoint_response(item))

    async def delete(self, endpoint_id: str) -> None:
        async with self._session_factory() as session:
            item = await session.get(WebhookEndpoint, endpoint_id)
            if item is None:
                raise WebhookError("webhook_not_found")
            await session.delete(item)
            await session.commit()

    async def enqueue_event(
        self,
        event_code: str,
        *,
        fields: dict[str, object] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        definition = get_event_definition(event_code)
        if definition is None:
            return 0
        safe_fields = {
            key: _safe_value(value)
            for key, value in (fields or {}).items()
            if key in definition.allowed_fields
        }
        event_id = "event_" + uuid4().hex
        now = datetime.now(UTC)
        payload = {
            "event_id": event_id,
            "event_code": definition.code,
            "schema_version": f"v{definition.version}",
            "occurred_at": now.isoformat(),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "task_id": task_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "status": _safe_value(safe_fields.get("status")),
            "summary": {
                "title_zh": definition.title_zh,
                "message_zh": definition.render(safe_fields),
                "fields": safe_fields,
            },
        }
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._session_factory() as session:
            endpoints = list(
                await session.scalars(
                    select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True))
                )
            )
            count = 0
            for endpoint in endpoints:
                codes = _decode_codes(endpoint.event_codes_json)
                if codes and event_code not in codes:
                    continue
                session.add(
                    WebhookDelivery(
                        id="delivery_" + uuid4().hex,
                        endpoint_id=endpoint.id,
                        event_id=event_id,
                        event_code=event_code,
                        payload_json=payload_json,
                        status="pending",
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                count += 1
            await session.commit()
        return count

    async def deliveries(self, *, endpoint_id: str | None = None, limit: int = 50) -> WebhookDeliveryListResponse:
        async with self._session_factory() as session:
            query = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
            if endpoint_id:
                query = query.where(WebhookDelivery.endpoint_id == endpoint_id)
            items = list(await session.scalars(query))
        return WebhookDeliveryListResponse(items=[_delivery_response(item) for item in items])

    async def publish_due(self, *, limit: int = 20) -> int:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(WebhookDelivery)
                    .where(
                        WebhookDelivery.status == "pending",
                        WebhookDelivery.next_attempt_at <= now,
                    )
                    .order_by(WebhookDelivery.next_attempt_at)
                    .limit(limit)
                )
            )
        delivered = 0
        for row in rows:
            if await self._deliver(row.id):
                delivered += 1
        return delivered

    async def retry_dead(self, delivery_id: str) -> WebhookDeliveryResponse:
        async with self._session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery is None:
                raise WebhookError("webhook_delivery_not_found")
            if delivery.status != "dead":
                raise WebhookError("webhook_delivery_conflict")
            delivery.status = "pending"
            delivery.attempts = 0
            delivery.next_attempt_at = datetime.now(UTC)
            delivery.last_error_code = None
            delivery.updated_at = datetime.now(UTC)
            await session.commit()
            return _delivery_response(delivery)

    async def _deliver(self, delivery_id: str) -> bool:
        async with self._session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery is None or delivery.status != "pending":
                return False
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
            if endpoint is None or not endpoint.enabled:
                delivery.status = "dead"
                delivery.last_error_code = "endpoint_disabled"
                delivery.updated_at = datetime.now(UTC)
                await session.commit()
                return False
            url = endpoint.url
            secret = self._crypto.decrypt(endpoint.secret_encrypted)
            payload = delivery.payload_json
            event_id = delivery.event_id
            attempt = delivery.attempts + 1
        try:
            await _assert_public_destination(url)
        except WebhookError as exc:
            await self._record_delivery_failure(delivery_id, attempt, None, exc.code, exc.code)
            return False
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{event_id}.{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            response = await self._http.post(
                url,
                content=payload.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "watch-assistant-webhook/1",
                    "X-Watch-Assistant-Event-Id": event_id,
                    "X-Watch-Assistant-Timestamp": timestamp,
                    "X-Watch-Assistant-Signature": f"v1={signature}",
                },
                timeout=DELIVERY_TIMEOUT,
            )
            status_code = response.status_code
            error_code = None if 200 <= status_code < 300 else "http_error"
            response_summary = f"http_{status_code}"
        except httpx.TooManyRedirects:
            status_code, error_code, response_summary = None, "redirect_blocked", "redirect_blocked"
        except httpx.HTTPError:
            status_code, error_code, response_summary = None, "request_failed", "request_failed"
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id) if delivery else None
            if delivery is None or endpoint is None:
                return False
            delivery.attempts = attempt
            delivery.last_status_code = status_code
            delivery.last_error_code = error_code
            delivery.last_response_summary = response_summary
            delivery.updated_at = now
            if error_code is None:
                delivery.status = "delivered"
                endpoint.last_success_at = now
                endpoint.failure_count = 0
                await session.commit()
                return True
            endpoint.last_failure_at = now
            endpoint.failure_count += 1
            if attempt >= MAX_ATTEMPTS:
                delivery.status = "dead"
            else:
                delivery.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[attempt - 1])
            await session.commit()
        return False

    async def _record_delivery_failure(
        self,
        delivery_id: str,
        attempt: int,
        status_code: int | None,
        error_code: str,
        summary: str,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id) if delivery else None
            if delivery is None or endpoint is None:
                return
            delivery.attempts = attempt
            delivery.last_status_code = status_code
            delivery.last_error_code = error_code
            delivery.last_response_summary = summary
            delivery.updated_at = now
            endpoint.last_failure_at = now
            endpoint.failure_count += 1
            if attempt >= MAX_ATTEMPTS:
                delivery.status = "dead"
            else:
                delivery.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[attempt - 1])
            await session.commit()


def _validate_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise WebhookError("webhook_url_not_allowed") from None
    if parsed.scheme.casefold() != "https" or not hostname or parsed.username or parsed.password:
        raise WebhookError("webhook_url_not_allowed")
    hostname = hostname.casefold().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"}:
        raise WebhookError("webhook_url_not_allowed")
    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
            if info[4] and info[4][0]
        }
    except (OSError, ValueError):
        raise WebhookError("webhook_url_unresolvable") from None
    if not addresses or any(_blocked_address(address) for address in addresses):
        raise WebhookError("webhook_url_not_allowed")
    return candidate


async def _assert_public_destination(value: str) -> None:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if not hostname:
        raise WebhookError("webhook_url_not_allowed")
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(
            None, socket.getaddrinfo, hostname, parsed.port or 443, 0, socket.SOCK_STREAM
        )
    except OSError:
        raise WebhookError("webhook_dns_failed") from None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except (IndexError, ValueError):
            raise WebhookError("webhook_dns_failed") from None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast:
            raise WebhookError("webhook_url_not_allowed")


def _blocked_address(address: ipaddress._BaseAddress) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_unspecified,
            address.is_multicast,
        )
    )


def _validate_event_codes(values: list[str]) -> list[str]:
    normalized = sorted({item.strip() for item in values if item.strip()})
    if any(len(item) > 128 or get_event_definition(item) is None for item in normalized):
        raise WebhookError("webhook_event_not_allowed")
    return normalized


def _decode_codes(value: str) -> set[str]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return set()
    return {item for item in decoded if isinstance(item, str)} if isinstance(decoded, list) else set()


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    return str(value)[:256]


def _endpoint_response(item: WebhookEndpoint) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        id=item.id,
        name=item.name,
        url=item.url,
        secret_prefix=item.secret_prefix,
        event_codes=sorted(_decode_codes(item.event_codes_json)),
        enabled=item.enabled,
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
        last_success_at=item.last_success_at,
        last_failure_at=item.last_failure_at,
        failure_count=item.failure_count,
    )


def _delivery_response(item: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        id=item.id,
        endpoint_id=item.endpoint_id,
        event_id=item.event_id,
        event_code=item.event_code,
        status=item.status,
        attempts=item.attempts,
        next_attempt_at=item.next_attempt_at,
        last_status_code=item.last_status_code,
        last_error_code=item.last_error_code,
        last_response_summary=item.last_response_summary,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


__all__ = ["WebhookError", "WebhookService"]
