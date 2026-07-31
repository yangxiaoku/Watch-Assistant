import json
from pathlib import Path

import httpx

from watch_assistant.cli import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_TIMEOUT,
    ApiClient,
    CliConfig,
    load_config,
    main,
    save_config,
)


def test_config_is_written_with_restricted_mode(tmp_path: Path):
    path = tmp_path / "config.json"
    save_config(path, CliConfig("http://app.test", "wa_at_secret"))
    assert load_config(path).server == "http://app.test"
    assert load_config(path).token == "wa_at_secret"
    assert path.read_text(encoding="utf-8").count("wa_at_secret") == 1


def test_system_status_has_stable_json_envelope(capsys, tmp_path: Path):
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            assert request.url.path == "/api/v1/health"
            return httpx.Response(200, json={"status": "ok"}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (  # type: ignore[method-assign]
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()  # type: ignore[method-assign]
    try:
        save_config(tmp_path / "config.json", CliConfig("http://app.test"))
        code = main(["--config", str(tmp_path / "config.json"), "--output", "json", "system", "status"])
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    output = capsys.readouterr().out
    body = json.loads(output)
    assert code == EXIT_OK
    assert body["ok"] is True
    assert body["api_version"] == "v1"
    assert body["data"]["status"] == "ok"


def test_backup_configuration_command_uses_redacted_configuration_endpoint(capsys):
    seen: list[str] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(request.url.path)
            return httpx.Response(
                200,
                json={"schema_version": 1, "requires_reconfiguration": ["p115_cookie"]},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "backup",
                "configuration",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["schema_version"] == 1
    assert seen == ["/api/v1/backups/configuration"]


def test_backup_configuration_import_command_confirms_and_uses_current_revisions(capsys, tmp_path: Path):
    seen: list[tuple[str, dict[str, object] | None]] = []
    configuration = {
        "schema_version": 1,
        "exported_at": "2026-07-31T00:00:00Z",
        "release": "test",
        "logging": {"level": "INFO", "retention_days": 14, "max_file_mb": 10, "revision": 4},
        "inspection": {"auto_start_enabled": True, "revision": 4},
        "content_policy": {"hide_adult_media": True, "hide_suspicious_resources": True, "hide_low_quality_resources": True, "blocked_keywords": [], "revision": 4},
        "organization": {
            "schedule_enabled": False, "scan_interval_minutes": 30, "source_directory_ids": [],
            "target_directory_id": None, "push_directory_id": None, "video_extensions": ["mkv"],
            "metadata_extensions": ["srt"], "rename_enabled": True, "media_probe_enabled": True,
            "ai_identification_enabled": False, "small_file_threshold_mb": 0, "cleanup_empty_directories": False,
            "strm_linkage_enabled": False, "operation_delay_seconds": 1.5, "include_children_category": False,
            "include_concert_category": False, "region_grouping_enabled": True, "year_grouping_enabled": False,
            "prefer_remux": True, "prefer_resolution": True, "prefer_dolby": False, "conflict_mode": 2,
            "multi_version_enabled": False, "revision": 4,
        },
        "notifications": {
            "enabled": True, "muted_event_codes": [], "quiet_hours_enabled": True,
            "quiet_hours_start": "23:00", "quiet_hours_end": "08:00", "quiet_hours_timezone": "Asia/Shanghai",
            "error_bypass_quiet_hours": True, "revision": 2,
        },
        "requires_reconfiguration": ["p115_cookie"],
    }
    file_path = tmp_path / "configuration.json"
    file_path.write_text(json.dumps(configuration), encoding="utf-8")

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.path == "/api/v1/backups/configuration":
                return httpx.Response(200, json={
                    "logging": {"revision": 9},
                    "notifications": {"revision": 6},
                }, request=request)
            payload = json.loads(request.content)
            seen.append((request.url.path, payload))
            return httpx.Response(200, json={"status": "imported"}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main([
            "--server", "http://app.test", "--token", "wa_at_test", "--output", "json",
            "backup", "configuration-import", "--file", str(file_path), "--confirm",
        ])
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["status"] == "imported"
    assert seen == [("/api/v1/backups/configuration/import", {
        **configuration,
        "expected_settings_revision": 9,
        "expected_notification_revision": 6,
        "confirmed": True,
    })]


def test_auth_failure_maps_to_exit_code(capsys, tmp_path: Path):
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(401, json={"error": {"code": "unauthorized"}}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        save_config(tmp_path / "config.json", CliConfig("http://app.test", "wa_at_secret"))
        code = main(["--config", str(tmp_path / "config.json"), "--output", "json", "system", "capabilities"])
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_AUTH
    assert body["error"]["code"] == "unauthorized"
    assert "wa_at_secret" not in capsys.readouterr().out


def test_structured_api_error_keeps_chinese_guidance_without_raw_detail(capsys, tmp_path: Path):
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "settings_conflict",
                        "title_zh": "设置已在其他位置更新",
                        "message_zh": "本次修改未保存。",
                        "suggestion_zh": "请加载最新设置后重新提交。",
                        "retryable": False,
                        "action": "reload_settings",
                        "request_id": "req_cli_error",
                    },
                    "detail": "internal exception should stay hidden",
                },
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "system",
                "status",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    output = capsys.readouterr().out
    body = json.loads(output)
    assert code == EXIT_CONFLICT
    assert body["error"] == {
        "code": "settings_conflict",
        "title_zh": "设置已在其他位置更新",
        "message_zh": "本次修改未保存。",
        "suggestion_zh": "请加载最新设置后重新提交。",
        "retryable": False,
        "action": "reload_settings",
    }
    assert "internal exception" not in output


def test_timeout_maps_to_exit_code():
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.ReadTimeout("timeout", request=request)

    client = ApiClient(CliConfig("http://app.test"), transport=Transport())
    try:
        try:
            client.get("/api/v1/health")
        except Exception as error:  # noqa: BLE001
            assert error.code == EXIT_TIMEOUT
    finally:
        client.close()


def test_read_only_library_media_and_audit_commands_use_versioned_resources(capsys):
    seen: list[str] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(request.url.path)
            if request.url.path == "/api/v1/libraries":
                return httpx.Response(200, json={"items": [], "next_cursor": None}, request=request)
            if request.url.path == "/api/v1/media/file:one":
                return httpx.Response(200, json={"media_id": "file:one"}, request=request)
            if request.url.path == "/api/v1/audit":
                return httpx.Response(200, json={"items": [], "next_cursor": None}, request=request)
            if request.url.path == "/api/v1/organization-plans":
                return httpx.Response(200, json={"items": [], "next_cursor": None}, request=request)
            return httpx.Response(404, json={"detail": "not_found"}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "library", "list"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "media", "show", "file:one"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["data"]["media_id"] == "file:one"
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "audit", "list"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "organize", "plans"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["ok"] is True
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    assert seen == ["/api/v1/libraries", "/api/v1/media/file:one", "/api/v1/audit", "/api/v1/organization-plans"]


def test_task_wait_polls_without_resubmitting(monkeypatch, capsys):
    responses = iter(
        [
            {"id": "task-one", "state": "queued"},
            {"id": "task-one", "state": "accepted"},
        ]
    )
    seen: list[tuple[str, str]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path))
            return httpx.Response(200, json=next(responses), request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    monkeypatch.setattr("watch_assistant.cli.time.sleep", lambda _: None)
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "task",
                "wait",
                "task-one",
                "--timeout",
                "1",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["state"] == "accepted"
    assert seen == [("GET", "/api/v1/tasks/task-one"), ("GET", "/api/v1/tasks/task-one")]


def test_task_cancel_uses_safe_cancel_endpoint(capsys):
    seen: list[tuple[str, str]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path))
            return httpx.Response(200, json={"id": "task-one", "state": "cancelled"}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "task",
                "cancel",
                "task-one",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["state"] == "cancelled"
    assert seen == [("POST", "/api/v1/tasks/task-one/cancel")]


def test_strm_status_uses_read_only_health_and_manifest_endpoints(capsys):
    seen: list[tuple[str, str, str]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path, request.url.query.decode()))
            if request.url.path == "/api/v1/health":
                return httpx.Response(
                    200,
                    json={"strm_capabilities": {"full": False, "playback": False}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"items": [], "page": 1, "page_size": 1, "total": 0, "total_pages": 0},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        for command in (
            ["strm", "status"],
            ["strm", "status", "--library", "library-one"],
        ):
            assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", *command]) == EXIT_OK
            assert json.loads(capsys.readouterr().out)["ok"] is True
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert seen == [
        ("GET", "/api/v1/health", ""),
        ("GET", "/api/v1/libraries/library-one/strm-manifest", "page=1&page_size=1"),
    ]


def test_strm_generate_and_sync_use_latest_scan_and_formal_write_endpoints(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "library_id": "library-one",
                        "latest_scan": {
                            "run_id": "scan-one",
                            "state": "completed",
                            "complete": True,
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"library_id": "library-one", "scan_run_id": "scan-one", "generated": 1},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        for command in (
            ["strm", "generate", "--library", "library-one", "--full"],
            ["strm", "sync", "--library", "library-one", "--workflow-id", "workflow-one"],
        ):
            assert main(
                ["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", *command]
            ) == EXIT_OK
            assert json.loads(capsys.readouterr().out)["ok"] is True
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert seen == [
        ("GET", "/api/v1/libraries/library-one", None),
        (
            "POST",
            "/api/v1/libraries/library-one/strm-generation",
            {"source_scan_run_id": "scan-one"},
        ),
        ("GET", "/api/v1/libraries/library-one", None),
        (
            "POST",
            "/api/v1/libraries/library-one/strm-incremental",
            {"source_scan_run_id": "scan-one", "workflow_id": "workflow-one"},
        ),
    ]


def test_strm_cleanup_plan_uses_latest_scan_without_delete_endpoint(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "library_id": "library-one",
                        "latest_scan": {
                            "run_id": "scan-one",
                            "state": "completed",
                            "complete": True,
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "plan_id": "strm_cleanup_one",
                    "library_id": "library-one",
                    "candidate_count": 1,
                    "executable_count": 1,
                    "blocked_count": 0,
                },
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        assert main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "strm",
                "cleanup-plan",
                "--library",
                "library-one",
            ]
        ) == EXIT_OK
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert json.loads(capsys.readouterr().out)["data"]["plan_id"] == "strm_cleanup_one"
    assert seen == [
        ("GET", "/api/v1/libraries/library-one", None),
        (
            "POST",
            "/api/v1/libraries/library-one/strm-cleanup-plan",
            {"source_scan_run_id": "scan-one"},
        ),
    ]


def test_strm_cleanup_apply_fetches_revision_and_requires_confirmation(capsys):
    digest = "a" * 64
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"plan_id": "strm_cleanup_one", "plan_hash": digest, "revision": 1},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "plan": {
                        "plan_id": "strm_cleanup_one",
                        "status": "applied",
                        "revision": 2,
                    },
                    "retired": 1,
                },
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        assert main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "strm",
                "cleanup-apply",
                "strm_cleanup_one",
                "--digest",
                digest,
                "--confirm",
                "--idempotency-key",
                "cleanup-key",
            ]
        ) == EXIT_OK
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert body["data"]["retired"] == 1
    assert body["data"]["idempotency_key"] == "cleanup-key"
    assert seen == [
        ("GET", "/api/v1/strm-cleanup-plans/strm_cleanup_one", None),
        (
            "POST",
            "/api/v1/strm-cleanup-plans/strm_cleanup_one/apply",
            {
                "expected_revision": 1,
                "digest": digest,
                "confirm": True,
                "idempotency_key": "cleanup-key",
            },
        ),
    ]


def test_strm_verify_uses_latest_scan_and_read_endpoint(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "library_id": "library-one",
                        "latest_scan": {
                            "run_id": "scan-one",
                            "state": "completed",
                            "complete": True,
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"status": "verified", "checked_count": 1, "issues": []},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        assert main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "strm",
                "verify",
                "--library",
                "library-one",
            ]
        ) == EXIT_OK
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert json.loads(capsys.readouterr().out)["data"]["status"] == "verified"
    assert seen == [
        ("GET", "/api/v1/libraries/library-one", None),
        (
            "POST",
            "/api/v1/libraries/library-one/strm-verify",
            {"source_scan_run_id": "scan-one"},
        ),
    ]


def test_workflow_actions_use_shared_approval_and_cancel_endpoints(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            return httpx.Response(
                200,
                json={"id": "wf-one", "status": "in_progress", "stages": []},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        for command in (
            ["workflow", "approve", "wf-one", "--reason", "用户确认"],
            ["workflow", "reject", "wf-one"],
            ["workflow", "cancel", "wf-one", "--reason", "用户取消"],
        ):
            code = main(
                ["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", *command]
            )
            assert code == EXIT_OK
            assert json.loads(capsys.readouterr().out)["ok"] is True
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert seen == [
        ("POST", "/api/v1/workflows/wf-one/approval", {"decision": "approve", "reason": "用户确认"}),
        ("POST", "/api/v1/workflows/wf-one/approval", {"decision": "reject"}),
        ("POST", "/api/v1/workflows/wf-one/cancel", {"reason": "用户取消"}),
    ]


def test_notification_commands_list_read_and_read_all_use_versioned_endpoints(capsys):
    seen: list[tuple[str, str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path, request.url.query.decode(), json.loads(request.content) if request.content else None))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"items": [{"id": "notice-one", "read": False}], "unread_count": 1},
                    request=request,
                )
            if request.url.path.endswith("/read-all"):
                return httpx.Response(200, json={"marked_count": 1}, request=request)
            return httpx.Response(200, json={"id": "notice-one", "read": True}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "notification", "list", "--unread-only"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["data"]["unread_count"] == 1
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "notification"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["data"]["unread_count"] == 1
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "notification", "read", "notice-one"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["data"]["read"] is True
        assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", "notification", "read-all"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["data"]["marked_count"] == 1
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert seen == [
        ("GET", "/api/v1/notifications", "unread_only=true", None),
        ("GET", "/api/v1/notifications", "", None),
        ("POST", "/api/v1/notifications/notice-one/read", "", {}),
        ("POST", "/api/v1/notifications/read-all", "", {}),
    ]


def test_webhook_test_command_only_enqueues_versioned_endpoint_test(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
            return httpx.Response(
                200,
                json={"id": "delivery-one", "event_code": "webhook.test", "status": "queued"},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "webhook",
                "test",
                "endpoint-one",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"] == {
        "id": "delivery-one",
        "event_code": "webhook.test",
        "status": "queued",
    }
    assert seen == [("POST", "/api/v1/webhooks/endpoint-one/test", {})]


def test_webhook_query_and_retry_commands_use_server_side_state_gate(capsys):
    seen: list[tuple[str, str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.method, request.url.path, request.url.query.decode(), json.loads(request.content) if request.content else None))
            if request.method == "GET" and request.url.path == "/api/v1/webhooks":
                return httpx.Response(200, json={"items": []}, request=request)
            if request.method == "GET":
                return httpx.Response(200, json={"items": []}, request=request)
            return httpx.Response(200, json={"id": "delivery-one", "status": "pending"}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        for command in (
            ["webhook", "list"],
            ["webhook", "deliveries", "--endpoint-id", "endpoint-one", "--limit", "10"],
            ["webhook", "retry", "delivery-one"],
        ):
            assert main(["--server", "http://app.test", "--token", "wa_at_test", "--output", "json", *command]) == EXIT_OK
            assert json.loads(capsys.readouterr().out)["ok"] is True
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    assert seen == [
        ("GET", "/api/v1/webhooks", "", None),
        ("GET", "/api/v1/webhooks/deliveries", "limit=10&endpoint_id=endpoint-one", None),
        ("POST", "/api/v1/webhooks/deliveries/delivery-one/retry", "", {}),
    ]


def test_organize_apply_sends_digest_confirmation_and_generated_key(monkeypatch, capsys):
    digest = "a" * 64
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(200, json={"plan_id": "plan-one", "revision": 3}, request=request)
            return httpx.Response(
                200,
                json={"operation_id": "op-one", "plan_id": "plan-one", "status": "planned"},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "organize",
                "apply",
                "plan-one",
                "--digest",
                digest,
                "--confirm",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["operation_id"] == "op-one"
    assert body["data"]["idempotency_key"].startswith("watchctl-")
    assert seen[0][:2] == ("GET", "/api/v1/organization-plans/plan-one")
    assert seen[1][0:2] == ("POST", "/api/v1/organization-plans/plan-one/operation")
    assert seen[1][2] == {
        "expected_revision": 3,
        "idempotency_key": body["data"]["idempotency_key"],
        "digest": digest,
        "confirm": True,
    }


def test_organize_apply_requires_explicit_confirmation(capsys):
    code = main(
        [
            "--server",
            "http://app.test",
            "--token",
            "wa_at_test",
            "--output",
            "json",
            "organize",
            "apply",
            "plan-one",
            "--digest",
            "a" * 64,
        ]
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 5
    assert body["error"]["code"] == "confirmation_required"


def test_organize_plan_uses_latest_scan_and_source_directory(capsys):
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, payload))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "library_id": "library-one",
                        "latest_scan": {
                            "run_id": "scan-one",
                            "state": "completed",
                            "complete": True,
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"plan_id": "plan-one", "status": "needs_review"},
                request=request,
            )

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "organize",
                "plan",
                "--library",
                "library-one",
                "--path-id",
                "directory-one",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["data"]["plan_id"] == "plan-one"
    assert seen == [
        ("GET", "/api/v1/libraries/library-one", None),
        (
            "POST",
            "/api/v1/libraries/library-one/organization-preview",
            {"source_scan_run_id": "scan-one", "source_directory_id": "directory-one"},
        ),
    ]


def test_organize_plan_requires_a_completed_scan(capsys):
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"library_id": "library-one", "latest_scan": None}, request=request)

    original = ApiClient.__init__
    original_close = ApiClient.close
    ApiClient.__init__ = lambda self, config: (
        setattr(self, "config", config),
        setattr(self, "_client", httpx.Client(base_url=config.server, transport=Transport())),
    )[-1]
    ApiClient.close = lambda self: self._client.close()
    try:
        code = main(
            [
                "--server",
                "http://app.test",
                "--token",
                "wa_at_test",
                "--output",
                "json",
                "organize",
                "plan",
                "--library",
                "library-one",
            ]
        )
    finally:
        ApiClient.__init__ = original
        ApiClient.close = original_close

    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFLICT
    assert body["error"]["code"] == "library_scan_required"
