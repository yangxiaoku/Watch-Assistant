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
