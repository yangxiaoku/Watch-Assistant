import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import WebhookEndpointCreateRequest
from watch_assistant.security import AuthContext
from watch_assistant.services.mcp import McpService
from watch_assistant.services.pwa_devices import PwaDeviceService
from watch_assistant.services.webhooks import WebhookError, WebhookService


async def _database():
    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    return database


@pytest.mark.asyncio
async def test_webhook_stores_secret_only_encrypted_and_enqueues_stable_event(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    database = await _database()
    service = WebhookService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
    )
    created = await service.create(
        WebhookEndpointCreateRequest(name="测试端点", url="https://example.com/hook")
    )
    assert created.secret.startswith("whsec_")
    assert created.secret not in created.item.model_dump_json()
    assert await service.enqueue_event("application.startup", fields={"status": "started"}) == 1
    deliveries = await service.deliveries()
    assert len(deliveries.items) == 1
    assert deliveries.items[0].event_id.startswith("event_")
    await service.aclose()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_webhook_rejects_private_targets_and_can_retry_dead_letter():
    database = await _database()
    service = WebhookService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
    )
    with pytest.raises(WebhookError, match="webhook_url_not_allowed"):
        await service.create(WebhookEndpointCreateRequest(name="内网", url="https://127.0.0.1/hook"))
    await service.aclose()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_mcp_read_only_scope_and_schema_contract():
    class Tasks:
        async def list_recent(self):
            return []

        async def get(self, _task_id):
            return None

    class Notifications:
        async def list(self, **_kwargs):
            return type("Response", (), {"items": [], "unread_count": 0})()

    service = McpService(task_service=Tasks(), notification_service=Notifications())
    context = AuthContext(identity="agent:test", via_bearer=True, scopes=frozenset({"task:read"}))
    response = await service.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "watch://tasks"}}, context=context, request_id="req_test")
    assert response["result"]["ok"] is True
    assert response["result"]["schema_version"] == "v1"
    forbidden = await service.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "watch://system/status"}}, context=context)
    assert forbidden["error"]["data"]["error_code"] == "missing_scope"


@pytest.mark.asyncio
async def test_pwa_device_subscription_is_encrypted_and_revocable():
    database = await _database()
    service = PwaDeviceService(database.session_factory, SecretCrypto(Fernet.generate_key().decode("ascii")))
    request = type("Request", (), {"name": "手机", "subscription": {"endpoint": "https://push.example.test/a", "expirationTime": None, "keys": {"p256dh": "public", "auth": "auth"}}})()
    device = await service.register("web-account", request)
    assert device.status == "active"
    assert len((await service.list("web-account")).items) == 1
    revoked = await service.revoke("web-account", device.id)
    assert revoked.status == "revoked"
    await database.engine.dispose()
