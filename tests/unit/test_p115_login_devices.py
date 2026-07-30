from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.services.p115_login_devices import (
    P115LoginDeviceError,
    P115LoginDeviceService,
)

COOKIE_ONE = "UID=uid-one; CID=cid-one; KID=kid-one; SEID=seid-one"
COOKIE_TWO = "UID=uid-two; CID=cid-two; KID=kid-two; SEID=seid-two"


@pytest.mark.asyncio
async def test_devices_are_encrypted_independent_sessions(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'devices.db'}")
    await initialize_database(database.engine)
    service = P115LoginDeviceService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
    )

    first = await service.add_device("电脑", "web", COOKIE_ONE)
    second = await service.add_device("平板", "ipad", COOKIE_TWO)
    devices = await service.list_devices()

    assert len(devices) == 2
    assert [item["active"] for item in devices].count(True) == 1
    assert next(item for item in devices if item["id"] == second["id"])["active"] is True
    assert await service.active_cookie() == COOKIE_TWO
    assert await service.get_cookie(str(first["id"])) == COOKIE_ONE
    await service.mark_active(str(first["id"]))
    assert await service.active_cookie() == COOKIE_ONE

    await service.revoke(str(second["id"]))
    remaining = await service.list_devices()
    assert [item["id"] for item in remaining] == [first["id"]]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_active_device_cannot_be_removed(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'devices.db'}")
    await initialize_database(database.engine)
    service = P115LoginDeviceService(
        database.session_factory,
        SecretCrypto(Fernet.generate_key().decode("ascii")),
    )
    device = await service.add_device("电脑", "web", COOKIE_ONE)

    with pytest.raises(P115LoginDeviceError, match="active_device_cannot_revoke"):
        await service.revoke(str(device["id"]))
    await database.engine.dispose()
