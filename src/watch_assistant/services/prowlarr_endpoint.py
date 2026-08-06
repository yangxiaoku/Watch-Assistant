"""Validation and DNS pinning policy for configured Prowlarr endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_PRIVATE_ENDPOINT_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


class ProwlarrEndpointRejected(ValueError):
    """The configured endpoint failed the explicit outbound policy."""


@dataclass(frozen=True, slots=True)
class ResolvedProwlarrEndpoint:
    """A normalized endpoint with one already-validated connection address."""

    base_url: str
    hostname: str
    port: int
    pinned_address: str


def normalize_prowlarr_base_url(value: object) -> str | None:
    """Normalize a Prowlarr base URL without accepting embedded credentials."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProwlarrEndpointRejected
    value = value.strip()
    if not value:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ProwlarrEndpointRejected
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProwlarrEndpointRejected from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        raise ProwlarrEndpointRejected
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if "%" in hostname:
            # Zone-scoped IPv6 literals are not a stable, exact endpoint.
            raise ProwlarrEndpointRejected
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, "", ""))


def parse_prowlarr_allowed_private_addresses(
    value: object,
) -> frozenset[str]:
    """Parse a comma-separated list of exact private IP addresses."""
    if value is None or value == "":
        return frozenset()
    if not isinstance(value, str):
        raise ProwlarrEndpointRejected

    tokens = [token.strip() for token in value.split(",")]
    if not tokens or any(not token for token in tokens):
        raise ProwlarrEndpointRejected

    addresses: set[str] = set()
    for token in tokens:
        if "/" in token or "*" in token:
            raise ProwlarrEndpointRejected
        try:
            address = ipaddress.ip_address(token)
        except ValueError as exc:
            raise ProwlarrEndpointRejected from exc
        if not _is_allowlistable_private_address(address):
            raise ProwlarrEndpointRejected
        addresses.add(str(address))
    return frozenset(addresses)


async def resolve_prowlarr_endpoint(
    base_url: str,
    allowed_private_addresses: Iterable[str] = (),
) -> ResolvedProwlarrEndpoint:
    """Resolve and validate every address before selecting one connection pin."""
    normalized = normalize_prowlarr_base_url(base_url)
    if normalized is None:
        raise ProwlarrEndpointRejected
    allowed = frozenset(allowed_private_addresses)
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise ProwlarrEndpointRejected from exc
    if not hostname or port is None:
        raise ProwlarrEndpointRejected

    literal_address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        pass

    if literal_address is not None:
        addresses = (literal_address,)
    else:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                0,
                socket.SOCK_STREAM,
            )
        except (OSError, UnicodeError) as exc:
            raise ProwlarrEndpointRejected from exc
        addresses = _unique_addresses(records)

    if not addresses or any(
        not _address_is_allowed(address, allowed) for address in addresses
    ):
        raise ProwlarrEndpointRejected
    return ResolvedProwlarrEndpoint(
        base_url=normalized,
        hostname=hostname,
        port=port,
        pinned_address=str(addresses[0]),
    )


def _unique_addresses(
    records: Iterable[tuple[object, ...]],
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for record in records:
        if len(record) < 5:
            continue
        try:
            address = ipaddress.ip_address(str(record[4][0]))
        except (IndexError, TypeError, ValueError) as exc:
            raise ProwlarrEndpointRejected from exc
        normalized = str(address)
        if normalized not in seen:
            seen.add(normalized)
            addresses.append(address)
    return tuple(addresses)


def _address_is_allowed(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowed_private_addresses: frozenset[str],
) -> bool:
    return _is_global_routable_address(address) or str(address) in allowed_private_addresses


def _is_allowlistable_private_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(address in network for network in _PRIVATE_ENDPOINT_NETWORKS)


def _is_global_routable_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return bool(
        address.is_global
        and not address.is_unspecified
        and not address.is_multicast
        and not address.is_reserved
    )
