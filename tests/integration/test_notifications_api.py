from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import NotificationSeverity


async def _make_client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, tmdb, pansou, app


@pytest.mark.integration
async def test_notifications_dedupe_read_state_and_preferences(tmp_path):
    client, database, tmdb, pansou, app = await _make_client(tmp_path)
    try:
        service = app.state.notification_service
        first = await service.notify(
            event_code="task.failed",
            severity=NotificationSeverity.ERROR,
            title_zh="任务失败",
            message_zh="任务失败，请重试",
            dedupe_key="task:one:failed",
            action_type="task",
            action_id="task_one",
        )
        second = await service.notify(
            event_code="task.failed",
            severity=NotificationSeverity.ERROR,
            title_zh="任务失败",
            message_zh="任务失败，请重试",
            dedupe_key="task:one:failed",
        )
        assert first is not None and second is not None
        assert first.id == second.id
        assert second.aggregate_count == 2
        assert "magnet:?" not in second.message_zh

        listed = await client.get("/api/v1/notifications")
        assert listed.status_code == 200
        assert listed.json()["unread_count"] == 1
        notification_id = listed.json()["items"][0]["id"]
        read = await client.post(f"/api/v1/notifications/{notification_id}/read")
        assert read.status_code == 200
        unread = await client.get(
            "/api/v1/notifications", params={"unread_only": True}
        )
        assert unread.json() == {"items": [], "unread_count": 0}

        preferences = await client.get("/api/v1/notification-preferences")
        assert preferences.status_code == 200
        assert preferences.json()["quiet_hours_enabled"] is True
        assert preferences.json()["quiet_hours_start"] == "23:00"
        assert preferences.json()["quiet_hours_end"] == "08:00"
        assert preferences.json()["quiet_hours_timezone"] == "Asia/Shanghai"
        assert preferences.json()["error_bypass_quiet_hours"] is True
        revision = preferences.json()["revision"]
        muted = await client.patch(
            "/api/v1/notification-preferences",
            json={
                "revision": revision,
                "muted_event_codes": ["task.failed"],
                "quiet_hours_start": "22:30",
                "quiet_hours_end": "07:30",
                "quiet_hours_timezone": "Asia/Tokyo",
                "error_bypass_quiet_hours": False,
            },
        )
        assert muted.status_code == 200
        assert muted.json()["quiet_hours_start"] == "22:30"
        assert muted.json()["quiet_hours_end"] == "07:30"
        assert muted.json()["quiet_hours_timezone"] == "Asia/Tokyo"
        assert muted.json()["error_bypass_quiet_hours"] is False
        invalid_clock = await client.patch(
            "/api/v1/notification-preferences",
            json={"revision": muted.json()["revision"], "quiet_hours_start": "25:00"},
        )
        assert invalid_clock.status_code == 422
        invalid_timezone = await client.patch(
            "/api/v1/notification-preferences",
            json={"revision": muted.json()["revision"], "quiet_hours_timezone": "Not/AZone"},
        )
        assert invalid_timezone.status_code == 422
        suppressed = await service.notify(
            event_code="task.failed",
            severity=NotificationSeverity.ERROR,
            title_zh="任务失败",
            message_zh="再次失败",
            dedupe_key="task:two:failed",
        )
        assert suppressed is None

        log_items, _ = await app.state.settings_service.log_store.list(
            cursor=None, limit=50, category=None
        )
        event_codes = {item["event_code"] for item in log_items}
        assert {
            "notification.created",
            "notification.aggregated",
            "notification.read",
            "notification.preferences_changed",
        } <= event_codes
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_business_events_create_actionable_notifications_without_log_noise(tmp_path):
    client, database, tmdb, pansou, app = await _make_client(tmp_path)
    try:
        await app.state.settings_service.log_event(
            "workflow.stage_changed",
            fields={"stage": "strm", "status": "failed", "error_code": "scan_failed"},
            correlation_id="corr_notice",
            task_id="wf_notice",
        )
        await app.state.settings_service.log_event(
            "application.startup", fields={"status": "started"}
        )
        response = await client.get("/api/v1/notifications")
        assert response.status_code == 200
        assert response.json()["unread_count"] == 1
        item = response.json()["items"][0]
        assert item["event_code"] == "workflow.stage_changed"
        assert item["severity"] == "error"
        assert item["action_type"] == "workflow"
        assert item["action_id"] == "wf_notice"
        assert "scan_failed" not in item["message_zh"]
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_high_value_business_events_are_filtered_and_notified(tmp_path):
    client, database, tmdb, pansou, app = await _make_client(tmp_path)
    try:
        await app.state.settings_service.log_event(
            "p115.readiness", fields={"status": "unavailable"}
        )
        await app.state.settings_service.log_event(
            "p115.readiness", fields={"status": "ready"}
        )
        await app.state.settings_service.log_event(
            "subscription.resources_observed",
            fields={"status": "new", "count": 2, "hidden_count": 1},
            resource_type="subscription",
            resource_id="sub_notice",
        )
        await app.state.settings_service.log_event(
            "subscription.resources_observed",
            fields={"status": "deduplicated", "count": 0, "hidden_count": 2},
            resource_type="subscription",
            resource_id="sub_notice",
        )
        await app.state.settings_service.log_event(
            "backup.failed", fields={"status": "failed", "error_code": "disk_full"}
        )
        response = await client.get("/api/v1/notifications")
        assert response.status_code == 200
        items = response.json()["items"]
        assert {item["event_code"] for item in items} == {
            "p115.readiness",
            "subscription.resources_observed",
            "backup.failed",
        }
        assert response.json()["unread_count"] == 3
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_failure_notification_matrix_covers_inspection_organization_and_strm(
    tmp_path,
):
    client, database, tmdb, pansou, app = await _make_client(tmp_path)
    try:
        await app.state.settings_service.log_event(
            "inspection.batch_failed",
            fields={"status": "failed", "error_code": "metadata_timeout"},
            counts={"hidden_count": 2},
        )
        await app.state.settings_service.log_event(
            "organize.automation.blocked",
            fields={
                "status": "blocked",
                "error_code": "scope_not_verified",
                "source_directory_id": "source-test",
                "message_zh": "范围尚未校验",
            },
        )
        await app.state.settings_service.log_event(
            "organize.operation.failed",
            fields={"status": "failed", "error_code": "target_conflict"},
            task_id="operation-test",
        )
        await app.state.settings_service.log_event(
            "strm.verify.completed",
            fields={"status": "issues"},
            counts={"count": 3},
            resource_type="library",
            resource_id="library-test",
        )
        await app.state.settings_service.log_event(
            "strm.verify.completed",
            fields={"status": "verified"},
            counts={"count": 3},
            resource_type="library",
            resource_id="library-test",
        )

        response = await client.get("/api/v1/notifications")
        assert response.status_code == 200
        items = response.json()["items"]
        assert {item["event_code"] for item in items} == {
            "inspection.batch_failed",
            "organize.automation.blocked",
            "organize.operation.failed",
            "strm.verify.completed",
        }
        assert all(item["severity"] == "error" for item in items)
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
