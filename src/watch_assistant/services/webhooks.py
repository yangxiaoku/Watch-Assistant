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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update
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
from watch_assistant.services.code_utils import decode_string_set as _decode_codes
from watch_assistant.services.event_catalog import get_event_definition
from watch_assistant.services.observability import EventLogger

# 需求默认值:失败后重试 8 次(即最多投递 9 次),8 个退避间隔全部使用。
MAX_RETRIES = 8
MAX_ATTEMPTS = MAX_RETRIES + 1
DELIVERY_TIMEOUT = 10.0
# 投递认领租约:进入 in_flight 后保留该时长,期间其他投递者不得重复 POST。
# 若投递进程崩溃/超时,行停留在 in_flight,由 publish_due 在租约过期后
# 收回重试(见 _reclaim_stale_in_flight)。
DELIVERY_LEASE_SECONDS = 60.0
RETRY_DELAYS = (5, 15, 60, 300, 900, 1800, 3600, 7200)


@dataclass(frozen=True)
class _DeliveryStats:
    total: int = 0
    delivered: int = 0
    pending: int = 0
    dead: int = 0
    failure_rate: float | None = None
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PinnedDestination:
    """One already-validated connection address for a webhook hostname.

    校验与连接共用同一解析结果:DNS-rebinding 域名(如 *.nip.io)在
    ``_resolve_public_destination`` 只解析一次,连接阶段由
    ``_PinnedWebhookTransport`` 直接连到已校验地址,不再二次解析。
    """

    hostname: str
    port: int
    address: str


class _PinnedWebhookTransport(httpx.AsyncBaseTransport):
    """Connect only to the pre-validated address of the request's pin.

    请求携带 ``watch_assistant_pin`` extension 时,把 URL 主机改写为固定
    地址并保持 Host 头与 TLS SNI 为原始主机名;未携带 pin 的请求(测试注入
    的普通 client)原样转发。内层 transport 关闭 trust_env,环境代理不得
    看到投递目标与载荷。
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pin = request.extensions.get("watch_assistant_pin")
        if isinstance(pin, _PinnedDestination):
            host_header = request.headers.get("host") or request.url.netloc.decode("ascii")
            port = request.url.port
            netloc = f"[{pin.address}]" if ":" in pin.address else pin.address
            if port is not None:
                netloc = f"{netloc}:{port}"
            request.url = request.url.copy_with(netloc=netloc.encode("ascii"))
            request.headers["Host"] = host_header
            if request.url.scheme == "https":
                request.extensions["sni_hostname"] = pin.hostname
        return await self._inner.handle_async_request(request)


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
        self._http = http_client or httpx.AsyncClient(
            transport=_PinnedWebhookTransport(), follow_redirects=False
        )
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def create(
        self, request: WebhookEndpointCreateRequest
    ) -> WebhookEndpointCreateResponse:
        url = await _validate_url_async(request.url)
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
            response = await _endpoint_response_for(session, item)
        return WebhookEndpointCreateResponse(secret=secret, item=response)

    async def list(self) -> WebhookEndpointListResponse:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(
                    select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc())
                )
            )
            responses = [await _endpoint_response_for(session, item) for item in items]
        return WebhookEndpointListResponse(items=responses)

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
                values["url"] = await _validate_url_async(values["url"])
            if "event_codes" in values:
                values["event_codes"] = _validate_event_codes(values["event_codes"])
                item.event_codes_json = json.dumps(values.pop("event_codes"), separators=(",", ":"))
            for key, value in values.items():
                setattr(item, key, value.strip() if key == "name" else value)
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
            return await _endpoint_response_for(session, item)

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
            response = await _endpoint_response_for(session, item)
            return WebhookEndpointCreateResponse(secret=secret, item=response)

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

    async def enqueue_test(self, endpoint_id: str) -> WebhookDeliveryResponse:
        """Queue one safe test delivery for exactly one configured endpoint."""
        definition = get_event_definition("webhook.test")
        if definition is None:  # pragma: no cover - catalog import invariant
            raise WebhookError("webhook_event_not_allowed")
        now = datetime.now(UTC)
        event_id = "event_" + uuid4().hex
        payload = {
            "event_id": event_id,
            "event_code": definition.code,
            "schema_version": f"v{definition.version}",
            "occurred_at": now.isoformat(),
            "resource_type": None,
            "resource_id": None,
            "task_id": None,
            "request_id": None,
            "correlation_id": None,
            "status": "test",
            "summary": {
                "title_zh": definition.title_zh,
                "message_zh": definition.render({}),
                "fields": {},
            },
        }
        async with self._session_factory() as session:
            endpoint = await session.get(WebhookEndpoint, endpoint_id)
            if endpoint is None:
                raise WebhookError("webhook_not_found")
            if not endpoint.enabled:
                raise WebhookError("webhook_endpoint_disabled")
            delivery = WebhookDelivery(
                id="delivery_" + uuid4().hex,
                endpoint_id=endpoint.id,
                event_id=event_id,
                event_code=definition.code,
                payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                status="pending",
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(delivery)
            await session.commit()
            return _delivery_response(delivery)

    async def deliveries(self, *, endpoint_id: str | None = None, limit: int = 50) -> WebhookDeliveryListResponse:
        async with self._session_factory() as session:
            query = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
            if endpoint_id:
                query = query.where(WebhookDelivery.endpoint_id == endpoint_id)
            items = list(await session.scalars(query))
        return WebhookDeliveryListResponse(items=[_delivery_response(item) for item in items])

    async def publish_due(self, *, limit: int = 20) -> int:
        now = datetime.now(UTC)
        # 先收回租约已过期的 in_flight 行:投递进程崩溃/超时时该行不会
        # 自行离开 in_flight,租约到期后恢复为 pending 以便重新投递。
        await self._reclaim_stale_in_flight(now)
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
            try:
                if await self._deliver(row.id):
                    delivered += 1
            except Exception:  # noqa: BLE001 - 单行投递异常不得中断整批
                await self._record_delivery_failure(
                    row.id, row.attempts + 1, None, "delivery_unavailable", "delivery_unavailable"
                )
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

    async def _reclaim_stale_in_flight(self, now: datetime) -> None:
        """把租约已过期的 in_flight 投递恢复为 pending。

        原子条件更新:只有 next_attempt_at(认领时写入的租约到期点)已过的
        in_flight 行才被收回;仍在有效租约内的行(正常投递中)不受影响。
        """
        async with self._session_factory() as session:
            await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "in_flight",
                    WebhookDelivery.next_attempt_at <= now,
                )
                .values(
                    status="pending",
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def _deliver(self, delivery_id: str) -> bool:
        # 原子认领:仅当行仍为 pending 时置为 in_flight 并写租约到期点。
        # rowcount != 1 表示该行已被并发投递者认领或已非 pending,直接返回
        # False,避免并发 publish_due 对同一行重复 POST。
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            claim = await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.status == "pending",
                )
                .values(
                    status="in_flight",
                    next_attempt_at=now + timedelta(seconds=DELIVERY_LEASE_SECONDS),
                    updated_at=now,
                )
            )
            if claim.rowcount != 1:
                return False
            await session.commit()
        async with self._session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery is None or delivery.status != "in_flight":
                return False
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
            if endpoint is None or not endpoint.enabled:
                delivery.status = "dead"
                delivery.last_error_code = "endpoint_disabled"
                delivery.updated_at = datetime.now(UTC)
                await session.commit()
                return False
            url = endpoint.url
            payload = delivery.payload_json
            event_id = delivery.event_id
            attempt = delivery.attempts + 1
        try:
            secret = self._crypto.decrypt(endpoint.secret_encrypted)
        except Exception:  # noqa: BLE001 - 密钥轮换/损坏时该行必须走失败记录,
            # 而不是把异常抛到 publish_due 中断整批,导致队头阻塞饿死其他端点。
            await self._record_delivery_failure(
                delivery_id, attempt, None, "secret_unavailable", "secret_unavailable"
            )
            return False
        try:
            pin = await _resolve_public_destination(url)
        except WebhookError as exc:
            await self._record_delivery_failure(delivery_id, attempt, None, exc.code, exc.code)
            return False
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{event_id}.{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        request = self._http.build_request(
            "POST",
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
        # 连接固定到校验阶段解析出的地址(防 DNS rebinding TOCTOU)。
        request.extensions["watch_assistant_pin"] = pin
        try:
            response = await self._http.send(request)
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
                # 认领后行处于 in_flight,失败重试必须回到 pending 才会被
                # publish_due 按退避窗口重新选中(否则只能等租约过期回收)。
                delivery.status = "pending"
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
                # 同上:认领后失败重试需回到 pending 以按退避窗口重新投递。
                delivery.status = "pending"
                delivery.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[attempt - 1])
            await session.commit()


async def _validate_url_async(value: str) -> str:
    """Validate a webhook URL without blocking the event loop.

    ``socket.getaddrinfo`` 可能阻塞数秒(解析超时),必须放进 executor;
    与投递侧 ``_resolve_public_destination`` 保持一致,避免在单 worker
    事件循环上做同步 DNS 造成整个服务冻结。
    """
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
        addresses = await asyncio.get_running_loop().run_in_executor(
            None,
            socket.getaddrinfo,
            hostname,
            port or 443,
            0,
            socket.SOCK_STREAM,
        )
    except OSError:
        raise WebhookError("webhook_url_unresolvable") from None
    seen: set[str] = set()
    for info in addresses:
        if not info[4] or not info[4][0]:
            continue
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise WebhookError("webhook_url_unresolvable") from None
        if str(address) in seen:
            continue
        seen.add(str(address))
        if _blocked_address(address):
            raise WebhookError("webhook_url_not_allowed")
    if not seen:
        raise WebhookError("webhook_url_unresolvable") from None
    return candidate


async def _resolve_public_destination(value: str) -> _PinnedDestination:
    """Resolve and validate every address, returning one connection pin.

    投递时只解析一次并固定结果:连接阶段由 ``_PinnedWebhookTransport``
    直连 ``address``,彻底消除校验与连接之间的 DNS-rebinding TOCTOU。
    """
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if not hostname:
        raise WebhookError("webhook_url_not_allowed")
    try:
        port = parsed.port or 443
    except ValueError:
        raise WebhookError("webhook_url_not_allowed") from None
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(
            None, socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM
        )
    except OSError:
        raise WebhookError("webhook_dns_failed") from None
    pinned: str | None = None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except (IndexError, ValueError):
            raise WebhookError("webhook_dns_failed") from None
        if _blocked_address(ip):
            raise WebhookError("webhook_url_not_allowed")
        if pinned is None:
            pinned = str(ip)
    if pinned is None:
        raise WebhookError("webhook_dns_failed") from None
    return _PinnedDestination(
        hostname=hostname.casefold().rstrip("."), port=port, address=pinned
    )


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




def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    return str(value)[:256]


async def _endpoint_response_for(
    session: AsyncSession, item: WebhookEndpoint
) -> WebhookEndpointResponse:
    status_counts = {
        status: int(count)
        for status, count in await session.execute(
            select(WebhookDelivery.status, func.count())
            .where(WebhookDelivery.endpoint_id == item.id)
            .group_by(WebhookDelivery.status)
        )
    }
    next_retry_at = await session.scalar(
        select(func.min(WebhookDelivery.next_attempt_at)).where(
            WebhookDelivery.endpoint_id == item.id,
            WebhookDelivery.status == "pending",
        )
    )
    next_retry_at = _as_utc(next_retry_at)
    delivered = status_counts.get("delivered", 0)
    dead = status_counts.get("dead", 0)
    pending = status_counts.get("pending", 0)
    terminal = delivered + dead
    stats = _DeliveryStats(
        total=delivered + dead + pending,
        delivered=delivered,
        pending=pending,
        dead=dead,
        failure_rate=dead / terminal if terminal else None,
        next_retry_at=next_retry_at,
    )
    return _endpoint_response(item, stats=stats)


def _endpoint_response(
    item: WebhookEndpoint, *, stats: _DeliveryStats | None = None
) -> WebhookEndpointResponse:
    stats = stats or _DeliveryStats()
    return WebhookEndpointResponse(
        id=item.id,
        name=item.name,
        url=item.url,
        secret_prefix=item.secret_prefix,
        event_codes=sorted(_decode_codes(item.event_codes_json)),
        enabled=item.enabled,
        health_status=_endpoint_health_status(item),
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
        last_success_at=item.last_success_at,
        last_failure_at=item.last_failure_at,
        failure_count=item.failure_count,
        delivery_total=stats.total,
        delivered_count=stats.delivered,
        pending_count=stats.pending,
        dead_letter_count=stats.dead,
        failure_rate=stats.failure_rate,
        next_retry_at=stats.next_retry_at,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _endpoint_health_status(item: WebhookEndpoint) -> str:
    if not item.enabled:
        return "disabled"
    if item.last_success_at is None and item.last_failure_at is None:
        return "unknown"
    if item.failure_count >= MAX_RETRIES:
        return "failed"
    if item.last_failure_at is None or item.last_success_at is not None and item.last_success_at >= item.last_failure_at:
        return "healthy"
    return "degraded"


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
