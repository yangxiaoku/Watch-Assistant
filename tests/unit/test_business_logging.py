from pathlib import Path

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import LoggingLevel, LoggingSettingsPatch
from watch_assistant.services.observability import emit_event
from watch_assistant.services.settings import SettingsService


@pytest.mark.asyncio
async def test_business_event_logging_is_whitelisted_and_level_updates_immediately(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )

    await service.log_event(
        "resources.page_served",
        level=LoggingLevel.DEBUG,
        fields={
            "page": 1,
            "hidden_count": 2,
            "hidden_suspicious": 1,
            "resource_name": "private title",
        },
    )
    items, _ = await service.log_store.list(cursor=None, limit=20, category=None)
    assert items == []

    current = await service.get_logging()
    await service.update_logging(
        LoggingSettingsPatch(
            revision=current.revision,
            level=LoggingLevel.DEBUG,
        )
    )
    await service.log_event(
        "resources.page_served",
        level=LoggingLevel.DEBUG,
        fields={
            "page": 1,
            "hidden_count": 2,
            "hidden_suspicious": 1,
            "resource_name": "private title",
        },
    )
    items, _ = await service.log_store.list(cursor=None, limit=20, category=None)
    assert any(item["message"].startswith("resources.page_served") for item in items)
    assert all("private title" not in item["message"] for item in items)
    assert all("resource_name" not in item["message"] for item in items)

    await database.engine.dispose()


@pytest.mark.asyncio
async def test_business_log_write_failure_is_swallowed(tmp_path: Path, monkeypatch):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )

    async def fail_append(**_kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(service.log_store, "append", fail_append)
    await service.log_event("search.completed", fields={"count": 1})

    class FailingLogger:
        async def log_event(self, *_args, **_kwargs):
            raise RuntimeError("must not escape")

    await emit_event(FailingLogger(), "search.failed")
    await database.engine.dispose()
