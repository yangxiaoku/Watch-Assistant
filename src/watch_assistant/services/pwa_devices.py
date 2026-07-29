"""Encrypted, revocable Web Push device registrations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import PwaDevice
from watch_assistant.schemas import (
    PwaDeviceListResponse,
    PwaDeviceRegisterRequest,
    PwaDeviceResponse,
)


class PwaDeviceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PwaDeviceService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], crypto: SecretCrypto) -> None:
        self._session_factory = session_factory
        self._crypto = crypto

    async def register(self, owner_identity: str, request: PwaDeviceRegisterRequest) -> PwaDeviceResponse:
        subscription = _validate_subscription(request.subscription)
        encoded = json.dumps(subscription, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            item = await session.scalar(select(PwaDevice).where(PwaDevice.subscription_digest == digest))
            if item is None:
                item = PwaDevice(
                    id="device_" + uuid4().hex,
                    owner_identity=owner_identity[:128],
                    name=request.name.strip(),
                    subscription_encrypted=self._crypto.encrypt(encoded),
                    subscription_digest=digest,
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(item)
            elif item.owner_identity != owner_identity:
                raise PwaDeviceError("pwa_device_conflict")
            else:
                item.name = request.name.strip()
                item.last_seen_at = now
                item.revoked_at = None
            await session.commit()
            return _response(item)

    async def list(self, owner_identity: str) -> PwaDeviceListResponse:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(
                    select(PwaDevice)
                    .where(PwaDevice.owner_identity == owner_identity)
                    .order_by(PwaDevice.last_seen_at.desc())
                )
            )
        return PwaDeviceListResponse(items=[_response(item) for item in items])

    async def revoke(self, owner_identity: str, device_id: str) -> PwaDeviceResponse:
        async with self._session_factory() as session:
            item = await session.get(PwaDevice, device_id)
            if item is None or item.owner_identity != owner_identity:
                raise PwaDeviceError("pwa_device_not_found")
            if item.revoked_at is None:
                item.revoked_at = datetime.now(UTC)
                await session.commit()
            return _response(item)


def _validate_subscription(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - {"endpoint", "expirationTime", "keys"}:
        raise PwaDeviceError("pwa_subscription_invalid")
    endpoint = value.get("endpoint")
    keys = value.get("keys")
    expiration = value.get("expirationTime")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
        raise PwaDeviceError("pwa_subscription_invalid")
    if not isinstance(keys, dict) or not isinstance(keys.get("p256dh"), str) or not isinstance(keys.get("auth"), str):
        raise PwaDeviceError("pwa_subscription_invalid")
    if expiration is not None and (isinstance(expiration, bool) or not isinstance(expiration, (int, float))):
        raise PwaDeviceError("pwa_subscription_invalid")
    return {"endpoint": endpoint, "expirationTime": expiration, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def _response(item: PwaDevice) -> PwaDeviceResponse:
    return PwaDeviceResponse(
        id=item.id,
        name=item.name,
        created_at=item.created_at,
        last_seen_at=item.last_seen_at,
        revoked_at=item.revoked_at,
        status="revoked" if item.revoked_at is not None else "active",
    )


__all__ = ["PwaDeviceError", "PwaDeviceService"]
