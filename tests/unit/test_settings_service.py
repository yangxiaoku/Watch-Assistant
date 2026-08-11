import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from watch_assistant.models import ApplicationSettings
from watch_assistant.schemas import LogCategory, LoggingLevel, LoggingSettingsPatch
from watch_assistant.services import settings as settings_module
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


def test_redact_log_message_covers_share_tracker_and_length_limit():
    message = (
        "https://115.com/s/share-code?password=secret "
        "share_code=abcd udp://tracker.example/announce?passkey=secret "
        "UID=uid CID=cid token=token-value magnet:?xt=urn:btih/"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "https://example.test/api?query=secret /var/lib/watch-assistant/file "
        + ("x" * 5000)
    )

    redacted = redact_log_message(message)

    for secret in (
        "share-code",
        "abcd",
        "tracker.example",
        "passkey=secret",
        "uid",
        "cid",
        "token-value",
        "magnet:?",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "query=secret",
        "/var/lib/watch-assistant/file",
    ):
        assert secret not in redacted
    assert "[REDACTED_SHARE]" in redacted
    assert "[REDACTED_TRACKER]" in redacted
    assert len(redacted) <= 4096
    assert redacted.endswith("[TRUNCATED]")


def test_redact_log_message_covers_share_code_variants():
    message = (
        "receive_code=synthetic-a receive-code: synthetic-b "
        "access_code=synthetic-c access-code: synthetic-d "
        "提取码：synthetic-e 提取码: synthetic-f "
        "share_code=synthetic-g extract_code=synthetic-h"
    )

    redacted = redact_log_message(message)

    for value in (
        "synthetic-a",
        "synthetic-b",
        "synthetic-c",
        "synthetic-d",
        "synthetic-e",
        "synthetic-f",
        "synthetic-g",
        "synthetic-h",
    ):
        assert value not in redacted
    assert "receive_code" not in redacted
    assert "receive-code" not in redacted
    assert "access_code" not in redacted
    assert "access-code" not in redacted
    assert "提取码" not in redacted
    assert redacted.count("[REDACTED_SHARE]") == 8


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
async def test_log_store_latest_first_cursor_and_category_filter(tmp_path: Path):
    store = LogStore(tmp_path)
    for category in (
        LogCategory.SYSTEM,
        LogCategory.SEARCH,
        LogCategory.SYSTEM,
        LogCategory.SEARCH,
        LogCategory.CACHE,
    ):
        await store.append(
            level=LoggingLevel.INFO,
            category=category,
            message=category.value,
            retention_days=1,
            max_file_mb=50,
        )

    first, cursor = await store.list(cursor=None, limit=2, category=None)
    second, next_cursor = await store.list(cursor=cursor, limit=2, category=None)
    third, final_cursor = await store.list(cursor=next_cursor, limit=2, category=None)

    assert [item["id"] for item in first] == [5, 4]
    assert [item["id"] for item in second] == [3, 2]
    assert [item["id"] for item in third] == [1]
    assert cursor == 4
    assert next_cursor == 2
    assert final_cursor is None
    assert len({item["id"] for item in first + second + third}) == 5

    category_first, category_cursor = await store.list(
        cursor=None, limit=1, category=LogCategory.SEARCH
    )
    category_second, category_next = await store.list(
        cursor=category_cursor, limit=1, category=LogCategory.SEARCH
    )
    assert [item["id"] for item in category_first] == [4]
    assert [item["id"] for item in category_second] == [2]
    assert category_cursor == 4
    assert category_next is None


@pytest.mark.asyncio
async def test_log_store_filters_actor_and_resource_dimensions(tmp_path: Path):
    store = LogStore(tmp_path)
    await store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.AGENT,
        message="agent event",
        actor_type="agent",
        actor_id="agent-1",
        resource_type="library",
        resource_id="library-1",
        retention_days=1,
        max_file_mb=50,
    )
    await store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.SYSTEM,
        message="system event",
        actor_type="system",
        actor_id="worker-1",
        resource_type="task",
        resource_id="task-1",
        retention_days=1,
        max_file_mb=50,
    )

    items, next_cursor = await store.list(
        cursor=None,
        limit=10,
        category=None,
        actor_type="agent",
        actor_id="agent-1",
        resource_type="library",
        resource_id="library-1",
    )

    assert next_cursor is None
    assert [item["message"] for item in items] == ["agent event"]


def _log_record(record_id: int, timestamp: str) -> dict[str, object]:
    return {
        "id": record_id,
        "timestamp": timestamp,
        "level": "INFO",
        "category": "system",
        "message": f"record-{record_id}",
    }


@pytest.mark.asyncio
async def test_log_store_order_ignores_mtime_and_invalid_rotations(tmp_path: Path):
    timestamp = datetime.now(UTC).isoformat()
    current = tmp_path / "watch-assistant.log"
    rotated = tmp_path / "watch-assistant.log.20260726120000-10"
    invalid = tmp_path / "watch-assistant.log.not-a-rotation"
    current.write_text(json.dumps(_log_record(11, timestamp)) + "\n", encoding="utf-8")
    rotated.write_text(json.dumps(_log_record(10, timestamp)) + "\n", encoding="utf-8")
    invalid.write_text(json.dumps(_log_record(99, timestamp)) + "\n", encoding="utf-8")
    same_mtime_ns = time.time_ns()
    for path in (current, rotated, invalid):
        os.utime(path, ns=(same_mtime_ns, same_mtime_ns))

    store = LogStore(tmp_path)
    await store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.SYSTEM,
        message="record-12",
        retention_days=1,
        max_file_mb=50,
    )

    items, next_cursor = await store.list(cursor=None, limit=10, category=None)

    assert [item["id"] for item in items] == [12, 11, 10]
    assert len({item["id"] for item in items}) == 3
    assert next_cursor is None


@pytest.mark.asyncio
async def test_rotated_logs_sort_same_timestamp_ids_numerically(tmp_path: Path):
    timestamp = datetime.now(UTC).isoformat()
    same_mtime_ns = time.time_ns()
    paths = [
        tmp_path / f"watch-assistant.log.20260726120000-{record_id}"
        for record_id in (9, 10, 11)
    ]
    for path in paths:
        record_id = int(path.name.rsplit("-", 1)[1])
        path.write_text(
            json.dumps(_log_record(record_id, timestamp)) + "\n", encoding="utf-8"
        )
        os.utime(path, ns=(same_mtime_ns, same_mtime_ns))

    items, next_cursor = await LogStore(tmp_path).list(
        cursor=None, limit=10, category=None
    )

    assert [item["id"] for item in items] == [11, 10, 9]
    assert next_cursor is None


@pytest.mark.asyncio
async def test_log_store_skips_corrupt_records(tmp_path: Path):
    timestamp = datetime.now(UTC).isoformat()
    records = [
        {
            "id": 1,
            "timestamp": timestamp,
            "level": "INFO",
            "category": "system",
            "message": "old",
        },
        "not json object",
        {
            "id": 3,
            "timestamp": "2026-01-01T00:00:00",
            "level": "INFO",
            "category": "system",
            "message": "naive",
        },
        {
            "id": 4,
            "timestamp": timestamp,
            "level": "BAD",
            "category": "system",
            "message": "level",
        },
        {
            "id": 5,
            "timestamp": timestamp,
            "level": "INFO",
            "category": "bad",
            "message": "category",
        },
        {
            "id": True,
            "timestamp": timestamp,
            "level": "INFO",
            "category": "system",
            "message": "bool",
        },
        {
            "id": 6,
            "timestamp": timestamp,
            "level": "INFO",
            "category": "cache",
            "message": "new",
        },
    ]
    (tmp_path / "watch-assistant.log").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    items, next_cursor = await LogStore(tmp_path).list(
        cursor=None, limit=10, category=None
    )

    assert [item["id"] for item in items] == [6, 1]
    assert next_cursor is None


@pytest.mark.asyncio
async def test_log_store_uses_threaded_io_and_initializes_id_once(
    tmp_path: Path, monkeypatch
):
    store = LogStore(tmp_path)
    to_thread_calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(settings_module.asyncio, "to_thread", spy_to_thread)
    latest_calls = 0
    original_latest = store._latest_id_sync

    def spy_latest():
        nonlocal latest_calls
        latest_calls += 1
        return original_latest()

    monkeypatch.setattr(store, "_latest_id_sync", spy_latest)
    for _ in range(3):
        await store.append(
            level=LoggingLevel.INFO,
            category=LogCategory.SYSTEM,
            message="safe",
            retention_days=1,
            max_file_mb=50,
        )
    await store.list(cursor=None, limit=10, category=None)

    assert latest_calls == 1
    assert to_thread_calls == [
        "_append_sync",
        "_append_sync",
        "_append_sync",
        "_list_sync",
    ]


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


@pytest.mark.asyncio
async def test_organization_settings_default_small_file_threshold_is_100mb(tmp_path: Path):
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.services.settings import SettingsService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'org-defaults.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )

    organization = await service.get_organization()

    assert organization.small_file_threshold_mb == 100.0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_initial_logging_settings_create_one_row(tmp_path: Path):
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.services.settings import SettingsService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )

    results = await asyncio.gather(*(service.get_logging() for _ in range(12)))

    assert all(result.revision == 0 for result in results)
    async with database.session_factory() as session:
        rows = (await session.execute(select(ApplicationSettings))).scalars().all()
    assert len(rows) == 1
    assert rows[0].revision == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_fake_patch_failure(
    tmp_path: Path, monkeypatch
):
    from watch_assistant.db import create_database, initialize_database
    from watch_assistant.services.settings import SettingsService

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await initialize_database(database.engine)
    service = SettingsService(
        database.session_factory,
        state_directory=tmp_path / "state",
    )
    initial = await service.get_logging()
    append_calls = 0

    async def fail_append(**_kwargs):
        nonlocal append_calls
        append_calls += 1
        raise OSError("must not escape")

    monkeypatch.setattr(service.log_store, "append", fail_append)
    updated = await service.update_logging(
        LoggingSettingsPatch(
            level=LoggingLevel.WARNING,
            revision=initial.revision,
        )
    )

    assert updated.revision == 1
    assert (await service.get_logging()).revision == 1
    assert append_calls == 1
    await database.engine.dispose()
