import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import respx

from watch_assistant.api.webhooks import list_deliveries
from watch_assistant.services import webhooks


def _addr(host: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))]


@pytest.mark.asyncio
async def test_webhook_url_rejects_dns_that_resolves_to_private_address(monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *args, **kwargs: _addr("10.0.0.8"))
    with pytest.raises(webhooks.WebhookError) as error:
        await webhooks._validate_url_async("https://hooks.example.test/events")
    assert error.value.code == "webhook_url_not_allowed"


@pytest.mark.asyncio
async def test_webhook_url_accepts_public_dns_result(monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *args, **kwargs: _addr("8.8.8.8"))
    assert (await webhooks._validate_url_async("https://hooks.example.test/events")).startswith("https://")


@pytest.mark.asyncio
async def test_webhook_url_rejects_unresolvable_host(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("dns failure")

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", fail)
    with pytest.raises(webhooks.WebhookError) as error:
        await webhooks._validate_url_async("https://hooks.example.test/events")
    assert error.value.code == "webhook_url_unresolvable"


@pytest.mark.asyncio
async def test_webhook_delivery_query_passes_filter_and_limit_to_service():
    seen: dict[str, object] = {}

    class Service:
        async def deliveries(self, **kwargs):
            seen.update(kwargs)
            return {"items": []}

    response = await list_deliveries(
        None,
        Service(),
        endpoint_id="endpoint-one",
        limit=10,
    )

    assert response == {"items": []}
    assert seen == {"endpoint_id": "endpoint-one", "limit": 10}


@pytest.mark.asyncio
async def test_pinned_transport_connects_to_validated_address_and_keeps_host_and_sni():
    """F1:连接阶段必须连到校验时固定的地址,Host 头与 TLS SNI 保持原始域名。"""
    import httpx

    from watch_assistant.services.webhooks import (
        _PinnedDestination,
        _PinnedWebhookTransport,
    )

    seen: dict[str, object] = {}

    class _Inner:
        async def handle_async_request(self, request):
            seen["url"] = str(request.url)
            seen["host"] = request.headers.get("host")
            seen["sni"] = request.extensions.get("sni_hostname")
            return SimpleNamespace(status_code=200)

    transport = _PinnedWebhookTransport()
    transport._inner = _Inner()  # type: ignore[assignment]
    request = httpx.Request("POST", "https://hooks.example.test:8443/events")
    request.extensions["watch_assistant_pin"] = _PinnedDestination(
        "hooks.example.test", 8443, "93.184.216.34"
    )
    await transport.handle_async_request(request)

    assert seen["url"] == "https://93.184.216.34:8443/events"
    assert seen["host"] == "hooks.example.test:8443"
    assert seen["sni"] == "hooks.example.test"


@pytest.mark.asyncio
async def test_resolve_public_destination_pins_first_public_address(monkeypatch):
    """F1:投递解析只发生一次并固定地址,后续连接不得再次解析域名。"""
    import httpx

    from watch_assistant.services import webhooks

    calls = 0

    def _rebinding_dns(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _addr("8.8.8.8")
        # 第二次解析(攻击者视角的 rebinding)落到内网地址,但连接不再解析。
        return _addr("127.0.0.1")

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _rebinding_dns)
    pin = await webhooks._resolve_public_destination("https://hooks.example.test/events")
    assert pin.address == "8.8.8.8"
    assert pin.hostname == "hooks.example.test"
    assert pin.port == 443

    seen: dict[str, object] = {}

    class _Inner:
        async def handle_async_request(self, request):
            seen["url"] = str(request.url)
            seen["host"] = request.headers.get("host")
            return SimpleNamespace(status_code=200)

    transport = webhooks._PinnedWebhookTransport()
    transport._inner = _Inner()  # type: ignore[assignment]
    request = httpx.Request("POST", "https://hooks.example.test/events")
    request.extensions["watch_assistant_pin"] = pin
    await transport.handle_async_request(request)

    assert seen["url"] == "https://8.8.8.8/events"
    assert seen["host"] == "hooks.example.test"
    # 连接阶段没有第二次 getaddrinfo(域名只在校验时解析一次)。
    assert calls == 1


@pytest.mark.asyncio
async def test_resolve_public_destination_rejects_rebinding_to_private(monkeypatch):
    """F1:解析结果只要含内网/保留地址即拒绝,不放过 rebinding 域名。"""
    from watch_assistant.services import webhooks

    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *args, **kwargs: _addr("169.254.169.254"),
    )
    with pytest.raises(webhooks.WebhookError) as error:
        await webhooks._resolve_public_destination("https://hooks.example.test/events")
    assert error.value.code == "webhook_url_not_allowed"


def test_webhook_endpoint_health_status_is_derived_from_delivery_history():
    now = datetime(2026, 7, 31, tzinfo=UTC)

    def endpoint(**values):
        defaults = {
            "enabled": True,
            "failure_count": 0,
            "last_success_at": None,
            "last_failure_at": None,
        }
        defaults.update(values)
        return SimpleNamespace(**defaults)

    assert webhooks._endpoint_health_status(endpoint()) == "unknown"
    assert webhooks._endpoint_health_status(endpoint(enabled=False)) == "disabled"
    assert webhooks._endpoint_health_status(endpoint(last_success_at=now)) == "healthy"
    assert webhooks._endpoint_health_status(endpoint(last_failure_at=now)) == "degraded"
    assert webhooks._endpoint_health_status(endpoint(failure_count=8, last_failure_at=now)) == "failed"
    assert webhooks._endpoint_health_status(
        endpoint(last_success_at=now, last_failure_at=now.replace(second=0))
    ) == "healthy"


@pytest.mark.asyncio
@respx.mock
async def test_publish_due_survives_secret_decrypt_failure_and_does_not_block_batch(tmp_path, monkeypatch):
    """密钥轮换/损坏导致某行解密失败时,该行必须走失败记录,
    而不是把异常抛到 publish_due 中断整批(队头阻塞会饿死所有端点)。"""
    import httpx

    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import WebhookDelivery, WebhookEndpoint
    from watch_assistant.services.webhooks import WebhookService

    class _FlakyCrypto:
        def encrypt(self, value: str) -> str:
            return value

        def decrypt(self, value: str) -> str:
            if value == "broken-secret":
                raise ValueError("InvalidToken: key rotation")
            return value

    monkeypatch.setattr(
        webhooks.socket, "getaddrinfo",
        lambda *args, **kwargs: _addr("8.8.8.8"),
    )

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'webhook.db'}")
    await initialize_database(database.engine)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    try:
        async with database.session_factory() as session:
            session.add(WebhookEndpoint(
                id="ep-broken", name="broken", url="https://hooks.example.test/broken",
                secret_encrypted="broken-secret", secret_prefix="whsec_", enabled=True,
            ))
            session.add(WebhookEndpoint(
                id="ep-ok", name="ok", url="https://hooks.example.test/ok",
                secret_encrypted="ok-secret", secret_prefix="whsec_", enabled=True,
            ))
            # 显式 flush:纯 FK 列(无 relationship)时 UOW 不保证插入顺序
            await session.flush()
            session.add(WebhookDelivery(
                id="delivery-broken", endpoint_id="ep-broken", event_id="evt-1",
                event_code="task.failed", payload_json='{"a":1}', status="pending",
                attempts=0, next_attempt_at=now, created_at=now,
            ))
            session.add(WebhookDelivery(
                id="delivery-ok", endpoint_id="ep-ok", event_id="evt-2",
                event_code="task.completed", payload_json='{"b":2}', status="pending",
                attempts=0, next_attempt_at=now, created_at=now,
            ))
            await session.commit()

        service = WebhookService(
            database.session_factory, _FlakyCrypto(),
            http_client=httpx.AsyncClient(follow_redirects=False),
        )
        route = respx.post("https://hooks.example.test/ok").mock(
            return_value=httpx.Response(200, text="ok")
        )
        try:
            # 修复前:_deliver 解密抛异常 → publish_due 中断 → 返回 0,健康行永远得不到投递
            delivered = await service.publish_due(limit=20)
        finally:
            await service.aclose()

        assert delivered == 1  # 健康行仍被投递
        assert route.called
        async with database.session_factory() as session:
            broken = await session.get(WebhookDelivery, "delivery-broken")
            ok = await session.get(WebhookDelivery, "delivery-ok")
        assert broken.last_error_code == "secret_unavailable"
        assert broken.attempts == 1
        assert broken.status == "pending"  # 退避重试,而非队头永久阻塞
        assert ok.status == "delivered"  # 健康行不受队头阻塞影响,投递成功
        assert ok.attempts == 1
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_eighth_retry_uses_last_backoff_delay_then_dead_letter(tmp_path, monkeypatch):
    """REQ-019 默认 8 次重试:第 8 次失败后进入死信,7200s 退避必须被使用。"""
    from datetime import timedelta

    import httpx

    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import WebhookDelivery, WebhookEndpoint
    from watch_assistant.services.webhooks import WebhookService

    class _IdentityCrypto:
        def encrypt(self, value: str) -> str:
            return value

        def decrypt(self, value: str) -> str:
            return value

    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *args, **kwargs: _addr("8.8.8.8"),
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'webhook-retries.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            WebhookEndpoint(
                id="ep-retry",
                name="retry",
                url="https://hooks.example.test/retry",
                secret_encrypted="retry-secret",
                secret_prefix="whsec_",
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            WebhookDelivery(
                id="delivery-retry",
                endpoint_id="ep-retry",
                event_id="evt-retry",
                event_code="task.failed",
                payload_json='{"a":1}',
                status="pending",
                attempts=7,
                next_attempt_at=now,
                created_at=now,
            )
        )
        await session.commit()

    route = respx.post("https://hooks.example.test/retry").mock(
        return_value=httpx.Response(500, text="boom")
    )
    service = WebhookService(
        database.session_factory,
        _IdentityCrypto(),
        http_client=httpx.AsyncClient(follow_redirects=False),
    )
    try:
        assert await service.publish_due(limit=20) == 0
        async with database.session_factory() as session:
            delivery = await session.get(WebhookDelivery, "delivery-retry")
        assert delivery.attempts == 8
        assert delivery.status == "pending"
        delay = delivery.next_attempt_at.replace(tzinfo=UTC) - now
        assert timedelta(seconds=7190) <= delay <= timedelta(seconds=7210)

        # 第 9 次投递(8 次重试后的最后一次尝试)失败进入死信。
        async with database.session_factory() as session:
            delivery = await session.get(WebhookDelivery, "delivery-retry")
            delivery.next_attempt_at = now
            await session.commit()
        assert await service.publish_due(limit=20) == 0
        async with database.session_factory() as session:
            delivery = await session.get(WebhookDelivery, "delivery-retry")
        assert delivery.attempts == 9
        assert delivery.status == "dead"
    finally:
        await service.aclose()
        await database.engine.dispose()

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_publish_does_not_duplicate_http_post(tmp_path, monkeypatch):
    """并发 publish_due 不得对同一行重复 POST:投递必须先原子认领。"""
    import asyncio

    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.models import WebhookDelivery, WebhookEndpoint
    from watch_assistant.services.webhooks import WebhookService

    class _IdentityCrypto:
        def encrypt(self, value: str) -> str:
            return value

        def decrypt(self, value: str) -> str:
            return value

    class _SlowHttp:
        def __init__(self):
            self.posts: list[str] = []
            self._block = asyncio.Event()

        def build_request(self, method, url, *, content, headers, timeout):
            return SimpleNamespace(
                method=method, url=url, content=content, headers=headers, extensions={}
            )

        async def send(self, request):
            self.posts.append(request.url)
            await self._block.wait()
            return SimpleNamespace(status_code=200)

        async def aclose(self):
            pass

    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *args, **kwargs: _addr("8.8.8.8"),
    )

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'webhook-dup.db'}")
    await initialize_database(database.engine)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    async with database.session_factory() as session:
        session.add(
            WebhookEndpoint(
                id="ep-dup",
                name="dup",
                url="https://hooks.example.test/dup",
                secret_encrypted="dup-secret",
                secret_prefix="whsec_",
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            WebhookDelivery(
                id="delivery-dup",
                endpoint_id="ep-dup",
                event_id="evt-dup",
                event_code="task.failed",
                payload_json='{"a":1}',
                status="pending",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
        )
        await session.commit()

    slow = _SlowHttp()
    service = WebhookService(
        database.session_factory, _IdentityCrypto(), http_client=slow
    )
    try:
        first = asyncio.create_task(service.publish_due())
        await asyncio.sleep(0)  # 让第一个进入 _deliver 并阻塞在 http.post
        second = asyncio.create_task(service.publish_due())
        await asyncio.sleep(0.1)  # 让第二个也尝试认领同一行
        slow._block.set()
        results = await asyncio.gather(first, second)
        assert len(slow.posts) == 1, f"重复 POST: {slow.posts}"
        assert results == [1, 0]
    finally:
        await service.aclose()
    await database.engine.dispose()
