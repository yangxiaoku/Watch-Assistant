import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

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
