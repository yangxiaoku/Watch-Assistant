from pathlib import Path

import pytest
from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import AuditRecord
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
    # 修复前无断言:append 抛 OSError 必须被吞掉,不得让 log_event 上抛。
    await service.log_event("search.completed", fields={"count": 1})  # 不应抛异常

    class FailingLogger:
        async def log_event(self, *_args, **_kwargs):
            raise RuntimeError("must not escape")

    await emit_event(FailingLogger(), "search.failed")  # 不应抛异常
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_unknown_business_event_is_recorded_as_structured_warning(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(database.session_factory, state_directory=tmp_path / "state")

    await service.log_event(
        "new.internal.event",
        request_id="req-unknown",
        correlation_id="corr-unknown",
    )

    items, _ = await service.log_store.list(cursor=None, limit=20, category=None)
    unknown = next(item for item in items if item["event_code"] == "observability.unknown_event")
    assert unknown["status"] == "unregistered"
    assert unknown["request_id"] == "req-unknown"
    assert unknown["correlation_id"] == "corr-unknown"
    assert unknown["context"] == {"event_code": "new.internal.event"}
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_structured_event_fields_and_filters_are_persisted(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(database.session_factory, state_directory=tmp_path / "state")

    await service.log_event(
        "search.completed",
        fields={"media_type": "电影", "count": 4, "duration_ms": 12, "status": "success"},
        request_id="req-test",
        correlation_id="corr-test",
        task_id="task-test",
    )
    items, cursor = await service.log_store.list(
        cursor=None,
        limit=10,
        category=None,
        event_code="search.completed",
        status="success",
        request_id="req-test",
        correlation_id="corr-test",
        task_id="task-test",
    )

    assert cursor is None
    assert len(items) == 1
    assert items[0]["title_zh"] == "资源搜索完成"
    assert "电影" in items[0]["message_zh"]
    assert items[0]["counts"] == {"count": 4}
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_library_events_render_registered_scope_fields(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(database.session_factory, state_directory=tmp_path / "state")

    await service.log_event(
        "library.configuration.changed",
        fields={"status": "saved", "scope_verified": False},
    )
    await service.log_event(
        "library.scope.verified",
        fields={"status": "verified", "enabled": True},
    )

    items, _ = await service.log_store.list(cursor=None, limit=10, category=None)
    assert [item["event_code"] for item in items] == [
        "library.scope.verified",
        "library.configuration.changed",
    ]
    assert all(item["status"] in {"saved", "verified"} for item in items)
    assert not any(item["event_code"] == "observability.unknown_event" for item in items)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_settings_change_has_durable_audit_record(tmp_path: Path):
    from watch_assistant.schemas import LoggingSettingsPatch

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}")
    await initialize_database(database.engine)
    service = SettingsService(database.session_factory, state_directory=tmp_path / "state")
    current = await service.get_logging()

    await service.update_logging(
        LoggingSettingsPatch(revision=current.revision, level=LoggingLevel.WARNING)
    )

    async with database.session_factory() as session:
        records = (await session.execute(select(AuditRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].event_code == "settings.changed"
    assert records[0].status == "logging"
    assert "level" in records[0].context_json
    await database.engine.dispose()
