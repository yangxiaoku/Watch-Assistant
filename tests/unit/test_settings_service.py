import os
from pathlib import Path

import pytest

from watch_assistant.schemas import LogCategory, LoggingLevel, LoggingSettingsPatch
from watch_assistant.services.settings import LogStore, redact_log_message


def test_redact_log_message_removes_sensitive_values():
    message = (
        "cookie=UID=uid; CID=cid; KID=kid; SEID=seid token=secret "
        "password=pass magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "https://example.test/api?token=query /var/lib/watch-assistant/file"
    )

    redacted = redact_log_message(message)

    assert "uid" not in redacted
    assert "secret" not in redacted
    assert "password=pass" not in redacted
    assert "magnet:?" not in redacted
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in redacted
    assert "query" not in redacted
    assert "/var/lib/watch-assistant/file" not in redacted


@pytest.mark.asyncio
async def test_log_store_rotates_redacts_and_paginates(tmp_path: Path):
    store = LogStore(tmp_path)
    message = "password=secret token=token-value " + ("x" * (1024 * 1024))

    await store.append(
        level=LoggingLevel.ERROR,
        category=LogCategory.SYSTEM,
        message=message,
        retention_days=1,
        max_file_mb=1,
    )
    await store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.SEARCH,
        message="safe",
        retention_days=1,
        max_file_mb=1,
    )

    items, next_cursor = await store.list(cursor=None, limit=1, category=None)
    assert len(items) == 1
    assert next_cursor == items[0]["id"]
    assert "secret" not in items[0]["message"]
    assert "token-value" not in items[0]["message"]
    if os.name != "nt":
        assert all(
            path.stat().st_mode & 0o077 == 0
            for path in tmp_path.glob("watch-assistant.log*")
        )


@pytest.mark.asyncio
async def test_settings_patch_persists_and_rejects_stale_revision(tmp_path):
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.services.settings import SettingsConflict, SettingsService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )

    initial = await service.get_logging()
    updated = await service.update_logging(
        LoggingSettingsPatch(
            level=LoggingLevel.WARNING,
            retention_days=7,
            max_file_mb=5,
            revision=initial.revision,
        )
    )
    assert updated.revision == initial.revision + 1
    assert (await service.get_logging()).level == LoggingLevel.WARNING
    with pytest.raises(SettingsConflict):
        await service.update_logging(
            LoggingSettingsPatch(revision=initial.revision, level=LoggingLevel.ERROR)
        )

    await database.engine.dispose()
