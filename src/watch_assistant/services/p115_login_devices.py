"""Encrypted storage for independently switchable 115 login devices."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import P115LoginDevice
from watch_assistant.services.p115_credentials import normalize_cookie_text


class P115LoginDeviceError(ValueError):
    pass


class P115LoginDeviceService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], crypto: SecretCrypto):
        self._session_factory = session_factory
        self._crypto = crypto

    async def list_devices(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(P115LoginDevice)
                .where(P115LoginDevice.revoked_at.is_(None))
                .order_by(P115LoginDevice.created_at.desc())
            )
            devices: Sequence[P115LoginDevice] = result.scalars().all()
        return [self._summary(device) for device in devices]

    async def active_cookie(self) -> str | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(P115LoginDevice)
                .where(P115LoginDevice.active.is_(True), P115LoginDevice.revoked_at.is_(None))
                .limit(1)
            )
            device = result.scalar_one_or_none()
        return self._decrypt(device.cookie_encrypted) if device is not None else None

    async def add_device(self, name: str, device_code: str, cookie: str) -> dict[str, object]:
        normalized = normalize_cookie_text(cookie)
        if normalized is None:
            raise P115LoginDeviceError("credential_rejected")
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise P115LoginDeviceError("invalid_device_name")
        device_code = device_code.strip().lower()
        if device_code not in {"web", "ios", "android", "ipad", "qios", "qipad", "qandroid"}:
            raise P115LoginDeviceError("invalid_device_code")
        now = datetime.now(UTC)
        device = P115LoginDevice(
            id=uuid4().hex,
            name=name,
            device_code=device_code,
            cookie_encrypted=self._crypto.encrypt(normalized),
            active=True,
            created_at=now,
            last_used_at=now,
        )
        async with self._session_factory() as session:
            await session.execute(
                P115LoginDevice.__table__.update()
                .where(P115LoginDevice.revoked_at.is_(None))
                .values(active=False)
            )
            session.add(device)
            await session.commit()
        return self._summary(device)

    async def get_cookie(self, device_id: str) -> str:
        async with self._session_factory() as session:
            device = await session.get(P115LoginDevice, device_id)
            if device is None or device.revoked_at is not None:
                raise P115LoginDeviceError("device_not_found")
            cookie_encrypted = device.cookie_encrypted
        cookie = self._decrypt(cookie_encrypted)
        if cookie is None:
            raise P115LoginDeviceError("device_unavailable")
        return cookie

    async def mark_active(self, device_id: str) -> None:
        async with self._session_factory() as session:
            device = await session.get(P115LoginDevice, device_id)
            if device is None or device.revoked_at is not None:
                raise P115LoginDeviceError("device_not_found")
            device.active = True
            device.last_used_at = datetime.now(UTC)
            await session.execute(
                P115LoginDevice.__table__.update()
                .where(P115LoginDevice.id != device_id, P115LoginDevice.revoked_at.is_(None))
                .values(active=False)
            )
            await session.commit()

    async def revoke(self, device_id: str) -> None:
        async with self._session_factory() as session:
            device = await session.get(P115LoginDevice, device_id)
            if device is None or device.revoked_at is not None:
                raise P115LoginDeviceError("device_not_found")
            if device.active:
                raise P115LoginDeviceError("active_device_cannot_revoke")
            device.revoked_at = datetime.now(UTC)
            await session.commit()

    def _decrypt(self, value: str) -> str | None:
        try:
            return normalize_cookie_text(self._crypto.decrypt(value))
        except Exception:  # noqa: BLE001 - credentials fail closed
            return None

    @staticmethod
    def _summary(device: P115LoginDevice) -> dict[str, object]:
        return {
            "id": device.id,
            "name": device.name,
            "device_code": device.device_code,
            "active": bool(device.active),
            "created_at": device.created_at,
            "last_used_at": device.last_used_at,
        }


__all__ = ["P115LoginDeviceError", "P115LoginDeviceService"]
