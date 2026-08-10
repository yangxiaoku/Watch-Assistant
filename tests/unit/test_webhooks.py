import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import respx

from watch_assistant.api.webhooks import list_deliveries
from watch_assistant.services import webhooks


def _addr(host: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))]


def test_webhook_url_rejects_dns_that_resolves_to_private_address(monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *args, **kwargs: _addr("10.0.0.8"))
    with pytest.raises(webhooks.WebhookError) as error:
        webhooks._validate_url("https://hooks.example.test/events")
    assert error.value.code == "webhook_url_not_allowed"


def test_webhook_url_accepts_public_dns_result(monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *args, **kwargs: _addr("8.8.8.8"))
    assert webhooks._validate_url("https://hooks.example.test/events").startswith("https://")


def test_webhook_url_rejects_unresolvable_host(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("dns failure")

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", fail)
    with pytest.raises(webhooks.WebhookError) as error:
        webhooks._validate_url("https://hooks.example.test/events")
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
