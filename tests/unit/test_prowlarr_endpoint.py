import socket

import pytest

from watch_assistant.services.prowlarr_endpoint import (
    ProwlarrEndpointRejected,
    normalize_prowlarr_base_url,
    parse_prowlarr_allowed_private_addresses,
    resolve_prowlarr_endpoint,
)


def _records(*addresses: str) -> list[tuple[object, ...]]:
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))
        for address in addresses
    ]


def test_private_allowlist_accepts_only_exact_private_ip_literals():
    assert parse_prowlarr_allowed_private_addresses(
        "127.0.0.1, 192.168.6.236, ::1"
    ) == frozenset({"127.0.0.1", "192.168.6.236", "::1"})

    for value in ("10.0.0.0/8", "10.0.*", "8.8.8.8", "0.0.0.0", ""):
        if value == "":
            assert parse_prowlarr_allowed_private_addresses(value) == frozenset()
        else:
            with pytest.raises(ProwlarrEndpointRejected):
                parse_prowlarr_allowed_private_addresses(value)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://prowlarr.test",
        "http://user:password@prowlarr.test",
        "http://prowlarr.test/search?secret=value",
        "http://prowlarr.test/search#fragment",
        "http://prowlarr.test/\nsearch",
        "http://prowlarr.test:65536",
    ],
)
def test_base_url_rejects_non_endpoint_syntax(value: str):
    with pytest.raises(ProwlarrEndpointRejected):
        normalize_prowlarr_base_url(value)


@pytest.mark.asyncio
async def test_private_literal_requires_exact_allowlist():
    with pytest.raises(ProwlarrEndpointRejected):
        await resolve_prowlarr_endpoint("http://127.0.0.1")

    resolved = await resolve_prowlarr_endpoint(
        "http://127.0.0.1", {"127.0.0.1"}
    )
    assert resolved.pinned_address == "127.0.0.1"


@pytest.mark.asyncio
async def test_dns_answers_must_all_be_global_or_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "watch_assistant.services.prowlarr_endpoint.socket.getaddrinfo",
        lambda *_args, **_kwargs: _records("192.168.6.236", "192.168.6.237"),
    )

    with pytest.raises(ProwlarrEndpointRejected):
        await resolve_prowlarr_endpoint(
            "http://prowlarr.test", {"192.168.6.236"}
        )


@pytest.mark.asyncio
async def test_dns_resolution_pins_one_validated_address(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "watch_assistant.services.prowlarr_endpoint.socket.getaddrinfo",
        lambda *_args, **_kwargs: _records("8.8.8.8", "1.1.1.1"),
    )

    resolved = await resolve_prowlarr_endpoint("http://prowlarr.test")

    assert resolved.hostname == "prowlarr.test"
    assert resolved.port == 80
    assert resolved.pinned_address == "8.8.8.8"
