import socket

import pytest

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
