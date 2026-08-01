from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import LogCategory, LoggingLevel
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "settings-web-password"
SCRIPT_TOKEN = "settings-script-token"


async def _app(tmp_path: Path, **feature_flags):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'settings-api.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        cookie_secure=False,
    )
    tmdb = type("Tmdb", (), {"aclose": lambda self: _noop()})()
    pansou = type("PanSou", (), {"aclose": lambda self: _noop()})()
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security,
        frontend_dir=tmp_path / "missing",
        **feature_flags,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return app, client, database, tmdb, pansou


async def _noop():
    return None


@pytest.mark.integration
async def test_settings_overview_logging_patch_and_redacted_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")
    app, client, database, tmdb, pansou = await _app(tmp_path)

    unauthorized = await client.get("/api/v1/settings/overview")
    assert unauthorized.status_code == 401
    login = await client.post(
        "/api/v1/auth/login",
        json={"password": WEB_PASSWORD},
    )
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    overview = await client.get("/api/v1/settings/overview")
    assert overview.status_code == 200
    assert overview.json()["release"] == "0123456"
    health = await client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["release"] == overview.json()["release"]
    assert set(overview.json()["capabilities"]) == {
        "inspection",
        "magnet",
        "share",
        "organization_plan",
        "organization_execution",
        "organization_write",
        "permanent_delete",
        "strm_full",
        "strm_incremental",
        "strm_cleanup",
        "strm_playback",
    }
    capabilities = overview.json()["capabilities"]
    assert all(
        capabilities[name] is False
        for name in (
            "organization_plan",
            "organization_execution",
            "organization_write",
            "permanent_delete",
            "strm_full",
            "strm_incremental",
            "strm_cleanup",
            "strm_playback",
        )
    )
    capability_statuses = overview.json()["capability_statuses"]
    assert capability_statuses["organization_plan"] == {
        "state": "unconfigured",
        "state_zh": "未配置",
        "last_success_at": None,
    }

    current = await client.get("/api/v1/settings/logging")
    assert current.status_code == 200
    revision = current.json()["revision"]
    patched = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "WARNING", "retention_days": 7, "revision": revision},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["level"] == "WARNING"
    assert patched.json()["revision"] == revision + 1

    conflict = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "ERROR", "revision": revision},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "settings_conflict"

    missing_csrf = await client.patch(
        "/api/v1/settings/logging",
        json={"level": "INFO", "revision": patched.json()["revision"]},
    )
    assert missing_csrf.status_code == 403

    await app.state.settings_service.log_store.append(
        level=LoggingLevel.INFO,
        category=LogCategory.SECURITY,
        message="password=secret token=token-value",
        actor_type="agent",
        actor_id="agent-test",
        resource_type="library",
        resource_id="library-test",
        retention_days=7,
        max_file_mb=5,
    )
    first_logs = await client.get("/api/v1/logs", params={"limit": 1})
    assert first_logs.status_code == 200
    assert len(first_logs.json()["items"]) == 1
    assert first_logs.json()["next_cursor"] is not None
    logs = await client.get(
        "/api/v1/logs",
        params={"limit": 2, "cursor": first_logs.json()["next_cursor"]},
    )
    assert logs.status_code == 200
    assert logs.json()["items"]
    assert logs.json()["items"][0]["id"] < first_logs.json()["items"][0]["id"]
    rendered = repr(first_logs.json()) + repr(logs.json())
    assert "secret" not in rendered
    assert "token-value" not in rendered

    filtered = await client.get(
        "/api/v1/logs",
        params={
            "actor_type": "agent",
            "actor_id": "agent-test",
            "resource_type": "library",
            "resource_id": "library-test",
        },
    )
    assert filtered.status_code == 200
    assert [item["resource_id"] for item in filtered.json()["items"]] == ["library-test"]

    exported = await client.get(
        "/api/v1/logs/export",
        params={"format": "csv", "event_code": "legacy.log"},
    )
    assert exported.status_code == 200
    assert "message_zh" in exported.text
    assert "secret" not in exported.text
    assert "token-value" not in exported.text

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_content_policy_defaults_requires_csrf_and_uses_revision(tmp_path):
    _app_obj, client, database, tmdb, pansou = await _app(tmp_path)
    try:
        assert (await client.get("/api/v1/settings/content-policy")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        csrf = login.json()["csrf_token"]
        response = await client.get("/api/v1/settings/content-policy")
        assert response.status_code == 200
        assert response.json() == {
            "hide_adult_media": True,
            "hide_suspicious_resources": True,
            "hide_low_quality_resources": True,
            "blocked_keywords": [],
            "revision": 0,
        }
        missing_csrf = await client.patch(
            "/api/v1/settings/content-policy",
            json={"revision": 0, "blocked_keywords": ["Foo"]},
        )
        assert missing_csrf.status_code == 403
        updated = await client.patch(
            "/api/v1/settings/content-policy",
            json={
                "revision": 0,
                "hide_adult_media": False,
                "blocked_keywords": ["ＦＯＯ－ＢＡＲ"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 1
        assert updated.json()["blocked_keywords"] == ["foo bar"]
        conflict = await client.patch(
            "/api/v1/settings/content-policy",
            json={"revision": 0},
            headers={"X-CSRF-Token": csrf},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "settings_conflict"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_logging_patch_rejects_unknown_fields(tmp_path):
    _app_instance, client, database, tmdb, pansou = await _app(tmp_path)
    response = await client.patch(
        "/api/v1/settings/logging",
        json={"revision": 0, "arbitrary": "value"},
        headers={"Authorization": f"Bearer {SCRIPT_TOKEN}"},
    )
    assert response.status_code == 422
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inspection_setting_persists_and_is_authenticated(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)
    assert (await client.get("/api/v1/settings/inspection")).status_code == 401
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    current = await client.get("/api/v1/settings/inspection")
    assert current.json() == {"auto_start_enabled": True, "revision": 0}
    patched = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": False, "revision": 0},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json() == {"auto_start_enabled": False, "revision": 1}
    conflict = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": True, "revision": 0},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "settings_conflict"
    missing_csrf = await client.patch(
        "/api/v1/settings/inspection",
        json={"auto_start_enabled": True, "revision": 1},
    )
    assert missing_csrf.status_code == 403
    rebuilt_security = SecurityManager(
        web_password_hash=app.state.security_manager._web_password_hash,
        script_token_hash=PasswordHash.recommended().hash(SCRIPT_TOKEN),
        cookie_secure=False,
    )
    rebuilt = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=rebuilt_security,
        frontend_dir=tmp_path / "missing-rebuilt",
    )
    rebuilt_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rebuilt), base_url="http://app.test"
    )
    persisted = await rebuilt_client.get(
        "/api/v1/settings/inspection", cookies=client.cookies
    )
    assert persisted.status_code == 200
    assert persisted.json()["auto_start_enabled"] is False
    health = await rebuilt_client.get("/api/v1/health")
    assert health.json()["inspection_auto_start_enabled"] is False
    await rebuilt_client.aclose()
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_strm_routes_enforce_independent_flags_and_reach_service(tmp_path):
    output_root = tmp_path / "strm"
    output_root.mkdir()
    app, client, database, tmdb, pansou = await _app(
        tmp_path, strm_output_root=output_root
    )
    payload = {"source_scan_run_id": "missing-scan"}
    try:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        disabled = {
            "/api/v1/libraries/library/strm-generation": "strm_full_disabled",
            "/api/v1/libraries/library/strm-incremental": "strm_incremental_disabled",
            "/api/v1/libraries/library/strm-cleanup": "strm_cleanup_disabled",
        }
        for path, code in disabled.items():
            response = await client.post(path, json=payload, headers=headers)
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == code

        app.state.strm_full_enabled = True
        app.state.strm_incremental_enabled = True
        app.state.strm_cleanup_enabled = True
        for path in disabled:
            response = await client.post(path, json=payload, headers=headers)
            assert response.status_code == 409
            assert response.json()["detail"] == "source_snapshot_not_ready"

        cleanup_plan = await client.post(
            "/api/v1/libraries/library/strm-cleanup-plan",
            json=payload,
            headers=headers,
        )
        assert cleanup_plan.status_code == 409
        assert cleanup_plan.json()["detail"] == "source_snapshot_not_ready"

        cleanup_apply = await client.post(
            "/api/v1/strm-cleanup-plans/missing-plan/apply",
            json={
                "expected_revision": 1,
                "digest": "a" * 64,
                "confirm": True,
                "idempotency_key": "cleanup-key",
            },
            headers=headers,
        )
        assert cleanup_apply.status_code == 404
        assert cleanup_apply.json()["detail"] == "plan_not_found"

        workflow_response = await client.post(
            "/api/v1/workflows", json={"media_type": "movie"}, headers=headers
        )
        assert workflow_response.status_code == 201
        workflow_id = workflow_response.json()["id"]
        for stage in (
            "discovery",
            "inspection",
            "approval",
            "push",
            "availability",
            "organization",
        ):
            advanced = await client.patch(
                f"/api/v1/workflows/{workflow_id}/stages/{stage}",
                json={"status": "succeeded"},
                headers=headers,
            )
            assert advanced.status_code == 200
        response = await client.post(
            "/api/v1/libraries/library/strm-incremental",
            json={**payload, "workflow_id": workflow_id},
            headers=headers,
        )
        assert response.status_code == 409
        workflow = await client.get(f"/api/v1/workflows/{workflow_id}")
        assert workflow.status_code == 200
        stage = next(
            item for item in workflow.json()["stages"] if item["stage"] == "strm"
        )
        assert stage["status"] == "failed"
        assert stage["child_type"] == "strm_operation"
        assert stage["child_id"] == "strm_missing-scan"
        notices = await client.get("/api/v1/notifications")
        assert notices.status_code == 200
        notice = next(
            item
            for item in notices.json()["items"]
            if item["event_code"] == "workflow.stage_changed"
        )
        assert notice["action_type"] == "workflow"
        assert notice["action_id"] == workflow_id
        assert notice["severity"] == "error"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_health_fails_closed_when_inspection_setting_read_fails(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)

    async def unavailable():
        raise RuntimeError("hidden storage detail")

    app.state.settings_service.get_inspection = unavailable
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["inspection_auto_start_enabled"] is False
    assert "hidden storage detail" not in response.text

    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_playback_flag_cannot_claim_capability_without_transport(tmp_path):
    _app_instance, client, database, tmdb, pansou = await _app(
        tmp_path,
        strm_playback_enabled=True,
        strm_playback_contract_verified=True,
    )
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    overview = await client.get("/api/v1/settings/overview", cookies=client.cookies)
    assert overview.status_code == 200
    assert overview.json()["capabilities"]["strm_playback"] is False
    health = await client.get("/api/v1/health")
    assert health.json()["strm_capabilities"]["playback"] is False
    await client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_organization_settings_persist_and_validate_cid_scope(tmp_path):
    _app_instance, client, database, tmdb, pansou = await _app(tmp_path)
    try:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        current = await client.get("/api/v1/settings/organization")
        assert current.status_code == 200
        assert current.json()["schedule_enabled"] is False
        assert current.json()["scan_interval_minutes"] == 30

        updated = await client.patch(
            "/api/v1/settings/organization",
            json={
                "revision": current.json()["revision"],
                "schedule_enabled": True,
                "scan_interval_minutes": 5,
                "source_directory_ids": ["3482085898508567892"],
                "target_directory_id": "2988794667098701570",
                "push_directory_id": "3988794667098701570",
                "video_extensions": ["MKV", "mp4"],
                "metadata_extensions": ["SRT", "nfo"],
                "operation_delay_seconds": 2.0,
            },
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["schedule_enabled"] is True
        assert updated.json()["video_extensions"] == ["mkv", "mp4"]
        assert updated.json()["metadata_extensions"] == ["srt", "nfo"]
        assert updated.json()["push_directory_id"] == "3988794667098701570"

        invalid = await client.patch(
            "/api/v1/settings/organization",
            json={
                "revision": updated.json()["revision"],
                "source_directory_ids": ["3482085898508567892"],
                "target_directory_id": "3482085898508567892",
            },
            headers=headers,
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"] == "source_target_same"

        cleared = await client.patch(
            "/api/v1/settings/organization",
            json={
                "revision": updated.json()["revision"],
                "target_directory_id": None,
                "push_directory_id": None,
            },
            headers=headers,
        )
        assert cleared.status_code == 200
        assert cleared.json()["target_directory_id"] is None
        assert cleared.json()["push_directory_id"] is None
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_organization_directory_settings_reject_unbrowsed_ids(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)
    try:
        app.state.organization_target_root_id = "100"
        app.state.p115_browsed_directory_ids = {"100", "200"}
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        current = await client.get("/api/v1/settings/organization")

        rejected = await client.patch(
            "/api/v1/settings/organization",
            json={
                "revision": current.json()["revision"],
                "push_directory_id": "300",
            },
            headers=headers,
        )
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["detail"] == "p115_directory_out_of_scope"

        accepted = await client.patch(
            "/api/v1/settings/organization",
            json={
                "revision": current.json()["revision"],
                "push_directory_id": "200",
            },
            headers=headers,
        )
        assert accepted.status_code == 200
        assert accepted.json()["push_directory_id"] == "200"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_organization_manual_run_is_independent_from_schedule_and_stop_clears_queue(tmp_path):
    app, client, database, tmdb, pansou = await _app(tmp_path)

    class FakeScheduler:
        def __init__(self):
            self.requested = 0
            self.stopped = 0

        async def request_run_now(self):
            self.requested += 1
            return "org_test_run"

        def stop_pending(self):
            self.stopped += 1

    scheduler = FakeScheduler()
    app.state.organization_scheduler = scheduler
    try:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        run_now = await client.post(
            "/api/v1/settings/organization/run-now", json={}, headers=headers
        )
        assert run_now.status_code == 200
        assert run_now.json()["queued"] is True
        assert run_now.json()["run_id"] == "org_test_run"
        assert scheduler.requested == 1

        result = await client.get("/api/v1/settings/organization/result")
        assert result.status_code == 200
        assert result.json()["status"] == "unknown"
        assert result.json()["available_statuses"] == [
            "unknown", "success", "skipped", "deleted", "replace", "failed"
        ]

        stopped = await client.post(
            "/api/v1/settings/organization/stop", json={}, headers=headers
        )
        assert stopped.status_code == 200
        assert stopped.json()["schedule_enabled"] is False
        assert stopped.json()["run_id"] is None
        assert scheduler.stopped == 1
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
