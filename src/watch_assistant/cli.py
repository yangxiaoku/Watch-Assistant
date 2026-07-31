"""The read-only ``watchctl`` command line client.

The CLI intentionally talks only to the versioned HTTP API.  It does not load
the application database, a 115 cookie, or any host command runner.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

CLI_VERSION = "0.1.0"
API_VERSION = "v1"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_FORBIDDEN = 6
EXIT_UNAVAILABLE = 7
EXIT_TIMEOUT = 8
EXIT_PARTIAL = 9


class CliFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int,
        error_code: str,
        title_zh: str | None = None,
        suggestion_zh: str | None = None,
        retryable: bool | None = None,
        action: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_code = error_code
        self.title_zh = title_zh or message
        self.message_zh = message
        self.suggestion_zh = suggestion_zh
        self.retryable = retryable if retryable is not None else code in {7, 8}
        self.action = action
        self.request_id = request_id


@dataclass(frozen=True)
class CliConfig:
    server: str
    token: str | None = None
    timeout: float = 10.0


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.environ.get("WATCHCTL_CONFIG")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "watch-assistant" / "config.json"


def load_config(path: Path) -> CliConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliFailure("未配置服务地址", code=EXIT_USAGE, error_code="not_configured") from None
    except (OSError, json.JSONDecodeError):
        raise CliFailure("本地配置不可读取", code=EXIT_USAGE, error_code="config_invalid") from None
    if not isinstance(raw, dict):
        raise CliFailure("本地配置格式无效", code=EXIT_USAGE, error_code="config_invalid")
    server = raw.get("server")
    token = raw.get("token")
    timeout = raw.get("timeout", 10.0)
    if not isinstance(server, str) or not _valid_server(server):
        raise CliFailure("服务地址无效", code=EXIT_USAGE, error_code="config_invalid")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise CliFailure("Token 配置无效", code=EXIT_USAGE, error_code="config_invalid")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise CliFailure("超时配置无效", code=EXIT_USAGE, error_code="config_invalid")
    return CliConfig(server.rstrip("/"), token, float(timeout))


def save_config(path: Path, config: CliConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {"server": config.server, "token": config.token, "timeout": config.timeout},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temporary.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


class ApiClient:
    def __init__(self, config: CliConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=config.server,
            timeout=config.timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "X-Watch-Assistant-Client-Version": f"watchctl/{CLI_VERSION}",
                **({"Authorization": f"Bearer {config.token}"} if config.token else {}),
            },
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException:
            raise CliFailure("请求超时", code=EXIT_TIMEOUT, error_code="request_timeout") from None
        except httpx.HTTPError:
            raise CliFailure("服务暂不可达", code=EXIT_UNAVAILABLE, error_code="service_unavailable") from None
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "invalid_json"}
        if response.status_code >= 400:
            raise _failure_from_response(response.status_code, body)
        if not isinstance(body, (dict, list)):
            raise CliFailure("服务响应格式无效", code=EXIT_UNAVAILABLE, error_code="invalid_response")
        return body

    def post(
        self, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.post(path, json=payload or {})
        except httpx.TimeoutException:
            raise CliFailure("请求超时", code=EXIT_TIMEOUT, error_code="request_timeout") from None
        except httpx.HTTPError:
            raise CliFailure("服务暂不可达", code=EXIT_UNAVAILABLE, error_code="service_unavailable") from None
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "invalid_json"}
        if response.status_code >= 400:
            raise _failure_from_response(response.status_code, body)
        if not isinstance(body, (dict, list)):
            raise CliFailure("服务响应格式无效", code=EXIT_UNAVAILABLE, error_code="invalid_response")
        return body


def _failure_from_response(status: int, body: Any) -> CliFailure:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}
    raw_code = error.get("code")
    if not isinstance(raw_code, str) and isinstance(body, dict):
        raw_code = body.get("detail")
    error_code = raw_code if isinstance(raw_code, str) else "api_error"
    messages = {
        401: "认证失败",
        403: "权限不足",
        404: "资源不存在",
        409: "版本或并发冲突",
        429: "请求过于频繁",
    }
    if status == 401:
        code = EXIT_AUTH
    elif status == 403:
        code = EXIT_FORBIDDEN
    elif status == 404:
        code = EXIT_NOT_FOUND
    elif status in {409, 412}:
        code = EXIT_CONFLICT
    elif status == 408 or status == 504:
        code = EXIT_TIMEOUT
    elif status >= 500 or status == 429:
        code = EXIT_UNAVAILABLE
    else:
        code = EXIT_USAGE
    fallback = messages.get(status, "请求失败")
    title = error.get("title_zh") if isinstance(error.get("title_zh"), str) else fallback
    message = error.get("message_zh") if isinstance(error.get("message_zh"), str) else title
    suggestion = error.get("suggestion_zh") if isinstance(error.get("suggestion_zh"), str) else None
    retryable = error.get("retryable") if isinstance(error.get("retryable"), bool) else None
    action = error.get("action") if isinstance(error.get("action"), str) else None
    request_id = error.get("request_id") if isinstance(error.get("request_id"), str) else None
    return CliFailure(
        message,
        code=code,
        error_code=error_code,
        title_zh=title,
        suggestion_zh=suggestion,
        retryable=retryable,
        action=action,
        request_id=request_id,
    )


def envelope(*, data: Any = None, request_id: str | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "api_version": API_VERSION,
        "request_id": request_id,
        "data": data,
        "warnings": warnings or [],
        "next_actions": [],
    }


def error_envelope(error: CliFailure) -> dict[str, Any]:
    return {
        "ok": False,
        "api_version": API_VERSION,
        "request_id": error.request_id,
        "data": None,
        "warnings": [],
        "next_actions": [],
        "error": {
            "code": error.error_code,
            "title_zh": error.title_zh,
            "message_zh": error.message_zh,
            "suggestion_zh": error.suggestion_zh,
            "retryable": error.retryable,
            "action": error.action,
        },
    }


def _request_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    value = body.get("request_id")
    return value if isinstance(value, str) else None


def _valid_server(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment


def _server_from_args(args: argparse.Namespace, path: Path) -> CliConfig:
    if args.server:
        if not _valid_server(args.server):
            raise CliFailure("服务地址无效", code=EXIT_USAGE, error_code="invalid_server")
        return CliConfig(args.server.rstrip("/"), args.token, args.timeout)
    config = load_config(path)
    return CliConfig(config.server, config.token, args.timeout if args.timeout != 10.0 else config.timeout)


def _data_for(path: str, body: Any) -> Any:
    if path == "/api/v1/health":
        return body
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _command(args: argparse.Namespace, path: Path) -> tuple[Any, int]:
    if args.command == "configure":
        if not _valid_server(args.server):
            raise CliFailure("服务地址无效", code=EXIT_USAGE, error_code="invalid_server")
        token = args.token
        if args.token_stdin:
            token = sys.stdin.readline().strip()
        if token is None:
            token = getpass.getpass("Agent Token（输入不会回显）：").strip()
        if not token:
            raise CliFailure("Token 不能为空", code=EXIT_USAGE, error_code="token_required")
        save_config(path, CliConfig(args.server.rstrip("/"), token, args.timeout))
        return envelope(data={"server": args.server.rstrip("/"), "token_configured": True}), EXIT_OK

    config = _server_from_args(args, path)
    client = ApiClient(config)
    try:
        if args.command == "system" and args.system_command == "status":
            body = client.get("/api/v1/health")
            return envelope(data=_data_for("/api/v1/health", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "system" and args.system_command == "capabilities":
            body = client.get("/api/v1/agent/capabilities")
            return envelope(data=_data_for("/api/v1/agent/capabilities", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "schema" and args.schema_command == "commands":
            body = client.get("/openapi.json")
            paths = body.get("paths", {}) if isinstance(body, dict) else {}
            return envelope(data={"api_version": API_VERSION, "commands": sorted(paths)}), EXIT_OK
        if args.command == "schema" and args.schema_command == "export":
            body = client.get("/openapi.json")
            return envelope(data=body), EXIT_OK
        if args.command == "doctor":
            checks: dict[str, Any] = {"config": "ok", "server": "failed", "token": "not_checked"}
            health = client.get("/api/v1/health")
            checks["server"] = "ok"
            try:
                capabilities = client.get("/api/v1/agent/capabilities")
            except CliFailure as error:
                if error.code == EXIT_AUTH:
                    checks["token"] = "invalid"
                else:
                    checks["token"] = "unavailable"
            else:
                checks["token"] = "ok"
                checks["capabilities"] = _data_for("/api/v1/agent/capabilities", capabilities)
            checks["https"] = config.server.startswith("https://")
            warnings = [] if checks["https"] else ["服务地址未使用 HTTPS，请仅在受信任网络中使用"]
            return envelope(data=checks, request_id=_request_id(health), warnings=warnings), EXIT_OK if checks["server"] == "ok" and checks["token"] == "ok" else EXIT_AUTH
        if args.command == "task":
            if args.task_command == "list":
                body = client.get("/api/v1/tasks")
                return envelope(data=_data_for("/api/v1/tasks", body), request_id=_request_id(body)), EXIT_OK
            if args.task_command == "show":
                body = client.get(f"/api/v1/tasks/{args.task_id}")
                return envelope(data=_data_for("/api/v1/tasks", body), request_id=_request_id(body)), EXIT_OK
            if args.task_command == "retry":
                body = client.post(f"/api/v1/tasks/{args.task_id}/retry")
                return envelope(data=_data_for("/api/v1/tasks", body), request_id=_request_id(body)), EXIT_OK
            if args.task_command == "cancel":
                body = client.post(f"/api/v1/tasks/{args.task_id}/cancel")
                return envelope(data=_data_for("/api/v1/tasks", body), request_id=_request_id(body)), EXIT_OK
            if args.task_command == "wait":
                return _wait_for_task(client, args.task_id, args.wait_timeout)
        if args.command == "workflow":
            if args.workflow_command == "list":
                body = client.get("/api/v1/workflows")
            elif args.workflow_command in {"approve", "reject"}:
                payload = {"decision": "approve" if args.workflow_command == "approve" else "reject"}
                if args.reason:
                    payload["reason"] = args.reason
                body = client.post(
                    f"/api/v1/workflows/{args.workflow_id}/approval",
                    payload=payload,
                )
            elif args.workflow_command == "cancel":
                body = client.post(
                    f"/api/v1/workflows/{args.workflow_id}/cancel",
                    payload={"reason": args.reason} if args.reason else {},
                )
            else:
                body = client.get(f"/api/v1/workflows/{args.workflow_id}")
            return envelope(data=_data_for("/api/v1/workflows", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "notification":
            notification_command = args.notification_command or "list"
            if notification_command == "list":
                unread_only = getattr(args, "unread_only", False)
                limit = getattr(args, "limit", 50)
                params = {"unread_only": "true"} if unread_only else None
                if limit != 50:
                    params = params or {}
                    params["limit"] = str(limit)
                body = client.get("/api/v1/notifications", params=params)
                return envelope(
                    data=_data_for("/api/v1/notifications", body),
                    request_id=_request_id(body),
                ), EXIT_OK
            if notification_command == "read":
                body = client.post(f"/api/v1/notifications/{args.notification_id}/read")
            else:
                body = client.post("/api/v1/notifications/read-all")
            return envelope(
                data=_data_for("/api/v1/notifications", body),
                request_id=_request_id(body),
            ), EXIT_OK
        if args.command == "webhook" and args.webhook_command == "test":
            body = client.post(f"/api/v1/webhooks/{args.endpoint_id}/test")
            return envelope(
                data=_data_for("/api/v1/webhooks", body),
                request_id=_request_id(body),
            ), EXIT_OK
        if args.command == "webhook":
            if args.webhook_command == "list":
                body = client.get("/api/v1/webhooks")
            elif args.webhook_command == "deliveries":
                params = {"limit": str(args.limit)}
                if args.endpoint_id:
                    params["endpoint_id"] = args.endpoint_id
                body = client.get("/api/v1/webhooks/deliveries", params=params)
            else:
                body = client.post(f"/api/v1/webhooks/deliveries/{args.delivery_id}/retry")
            return envelope(
                data=_data_for("/api/v1/webhooks", body),
                request_id=_request_id(body),
            ), EXIT_OK
        if args.command == "backup":
            body = client.get("/api/v1/backups")
            return envelope(data=_data_for("/api/v1/backups", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "deployment":
            body = client.get("/api/v1/deployment/diagnostics")
            return envelope(data=_data_for("/api/v1/deployment/diagnostics", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "library":
            if args.library_command == "list":
                body = client.get(
                    "/api/v1/libraries",
                    params={"cursor": str(args.cursor), "limit": str(args.limit)},
                )
            else:
                body = client.get(f"/api/v1/libraries/{args.library_id}")
            return envelope(data=_data_for("/api/v1/libraries", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "media":
            if args.media_command == "list":
                params = {"cursor": str(args.cursor), "limit": str(args.limit)}
                if args.library:
                    params["library"] = args.library
                body = client.get("/api/v1/media", params=params)
            else:
                body = client.get(f"/api/v1/media/{args.media_id}")
            return envelope(data=_data_for("/api/v1/media", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "audit":
            if args.audit_command == "list":
                params = {"cursor": str(args.cursor), "limit": str(args.limit)}
                if args.actor:
                    params["actor"] = args.actor
                if args.event_code:
                    params["event_code"] = args.event_code
                if args.since:
                    params["since"] = args.since
                body = client.get("/api/v1/audit", params=params)
            else:
                body = client.get(f"/api/v1/audit/{args.audit_id}")
            return envelope(data=_data_for("/api/v1/audit", body), request_id=_request_id(body)), EXIT_OK
        if args.command == "organize":
            if args.organize_command == "plan":
                library = client.get(f"/api/v1/libraries/{args.library_id}")
                library_data = _data_for("/api/v1/libraries", library)
                latest_scan = library_data.get("latest_scan") if isinstance(library_data, dict) else None
                if not isinstance(latest_scan, dict):
                    raise CliFailure(
                        "媒体库没有可用的完整扫描",
                        code=EXIT_CONFLICT,
                        error_code="library_scan_required",
                        suggestion_zh="请先完成媒体库扫描后再生成整理计划。",
                    )
                scan_run_id = latest_scan.get("run_id")
                if (
                    not isinstance(scan_run_id, str)
                    or latest_scan.get("state") != "completed"
                    or latest_scan.get("complete") is not True
                ):
                    raise CliFailure(
                        "媒体库扫描尚未完成",
                        code=EXIT_CONFLICT,
                        error_code="library_scan_required",
                        suggestion_zh="请等待完整扫描完成后再生成整理计划。",
                    )
                payload = {"source_scan_run_id": scan_run_id}
                if args.path_id:
                    payload["source_directory_id"] = args.path_id
                body = client.post(
                    f"/api/v1/libraries/{args.library_id}/organization-preview",
                    payload=payload,
                )
                return envelope(
                    data=_data_for("/api/v1/libraries", body),
                    request_id=_request_id(body),
                ), EXIT_OK
            if args.organize_command == "plans":
                params = {"cursor": str(args.cursor), "limit": str(args.limit)}
                if args.status:
                    params["status"] = args.status
                body = client.get("/api/v1/organization-plans", params=params)
                return envelope(data=_data_for("/api/v1/organization-plans", body), request_id=_request_id(body)), EXIT_OK
            if args.organize_command == "show":
                body = client.get(f"/api/v1/organization-plans/{args.plan_id}")
                return envelope(data=_data_for("/api/v1/organization-plans", body), request_id=_request_id(body)), EXIT_OK
            if args.organize_command == "apply":
                if not args.confirm:
                    raise CliFailure(
                        "缺少确认参数",
                        code=EXIT_CONFLICT,
                        error_code="confirmation_required",
                    )
                plan = client.get(f"/api/v1/organization-plans/{args.plan_id}")
                plan_data = _data_for("/api/v1/organization-plans", plan)
                if not isinstance(plan_data, dict):
                    raise CliFailure("服务响应格式无效", code=EXIT_UNAVAILABLE, error_code="invalid_response")
                expected_revision = plan_data.get("revision")
                if not isinstance(expected_revision, int):
                    raise CliFailure("计划版本无效", code=EXIT_CONFLICT, error_code="plan_revision_changed")
                key = args.idempotency_key or f"watchctl-{uuid.uuid4().hex}"
                body = client.post(
                    f"/api/v1/organization-plans/{args.plan_id}/operation",
                    payload={
                        "expected_revision": expected_revision,
                        "idempotency_key": key,
                        "digest": args.digest,
                        "confirm": True,
                    },
                )
                data = _data_for("/api/v1/organization-plans", body)
                if isinstance(data, dict):
                    data = {**data, "idempotency_key": key}
                return envelope(data=data, request_id=_request_id(body)), EXIT_OK
        if args.command == "strm" and args.strm_command == "status":
            if args.library:
                body = client.get(
                    f"/api/v1/libraries/{args.library}/strm-manifest",
                    params={"page": "1", "page_size": "1"},
                )
                manifest = _data_for("/api/v1/libraries", body)
                data = {"library_id": args.library, "manifest": manifest}
            else:
                body = client.get("/api/v1/health")
                health = _data_for("/api/v1/health", body)
                data = {
                    "strm_capabilities": (
                        health.get("strm_capabilities", {})
                        if isinstance(health, dict)
                        else {}
                    )
                }
            return envelope(data=data, request_id=_request_id(body)), EXIT_OK
        if args.command == "strm" and args.strm_command == "cleanup-plan":
            library = client.get(f"/api/v1/libraries/{args.library_id}")
            library_data = _data_for("/api/v1/libraries", library)
            latest_scan = library_data.get("latest_scan") if isinstance(library_data, dict) else None
            scan_run_id = latest_scan.get("run_id") if isinstance(latest_scan, dict) else None
            if (
                not isinstance(scan_run_id, str)
                or latest_scan.get("state") != "completed"
                or latest_scan.get("complete") is not True
            ):
                raise CliFailure(
                    "媒体库没有可用的完整扫描",
                    code=EXIT_CONFLICT,
                    error_code="library_scan_required",
                    suggestion_zh="请先完成完整扫描后再生成 STRM 清理计划。",
                )
            body = client.post(
                f"/api/v1/libraries/{args.library_id}/strm-cleanup-plan",
                payload={"source_scan_run_id": scan_run_id},
            )
            return envelope(
                data=_data_for("/api/v1/libraries", body),
                request_id=_request_id(body),
            ), EXIT_OK
        if args.command == "strm" and args.strm_command in {"generate", "sync"}:
            library = client.get(f"/api/v1/libraries/{args.library_id}")
            library_data = _data_for("/api/v1/libraries", library)
            latest_scan = library_data.get("latest_scan") if isinstance(library_data, dict) else None
            scan_run_id = latest_scan.get("run_id") if isinstance(latest_scan, dict) else None
            if (
                not isinstance(scan_run_id, str)
                or latest_scan.get("state") != "completed"
                or latest_scan.get("complete") is not True
            ):
                raise CliFailure(
                    "媒体库没有可用的完整扫描",
                    code=EXIT_CONFLICT,
                    error_code="library_scan_required",
                    suggestion_zh="请先完成媒体库扫描后再同步 STRM。",
                )
            payload: dict[str, Any] = {"source_scan_run_id": scan_run_id}
            if args.workflow_id:
                payload["workflow_id"] = args.workflow_id
            endpoint = "strm-generation" if args.strm_command == "generate" else "strm-incremental"
            body = client.post(
                f"/api/v1/libraries/{args.library_id}/{endpoint}",
                payload=payload,
            )
            return envelope(
                data=_data_for("/api/v1/libraries", body),
                request_id=_request_id(body),
            ), EXIT_OK
        raise CliFailure("命令尚未开放", code=EXIT_UNAVAILABLE, error_code="command_unavailable")
    finally:
        client.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchctl", description="Watch Assistant 受控 Agent CLI")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--server", help="服务地址，仅用于本次调用")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--output", choices=("human", "json", "jsonl"), default="human")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="保存服务地址和 Agent Token")
    configure.add_argument("--server", required=True)
    configure.add_argument("--token", help=argparse.SUPPRESS)
    configure.add_argument("--token-stdin", action="store_true")
    configure.add_argument("--timeout", type=float, default=10.0)

    system = sub.add_parser("system", help="系统状态与能力")
    system_sub = system.add_subparsers(dest="system_command", required=True)
    system_sub.add_parser("status")
    system_sub.add_parser("capabilities")

    schema = sub.add_parser("schema", help="API Schema 发现")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_sub.add_parser("commands")
    schema_sub.add_parser("export")

    sub.add_parser("doctor", help="本地和服务诊断")

    task = sub.add_parser("task", help="任务只读查询")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_sub.add_parser("list")
    show = task_sub.add_parser("show")
    show.add_argument("task_id")
    wait = task_sub.add_parser("wait")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", dest="wait_timeout", type=float, default=60.0)
    retry = task_sub.add_parser("retry")
    retry.add_argument("task_id")
    cancel = task_sub.add_parser("cancel")
    cancel.add_argument("task_id")

    workflow = sub.add_parser("workflow", help="工作流只读查询")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_sub.add_parser("list")
    workflow_show = workflow_sub.add_parser("show")
    workflow_show.add_argument("workflow_id")
    workflow_approve = workflow_sub.add_parser("approve")
    workflow_approve.add_argument("workflow_id")
    workflow_approve.add_argument("--reason", help="操作原因（最多 255 字符）")
    workflow_reject = workflow_sub.add_parser("reject")
    workflow_reject.add_argument("workflow_id")
    workflow_reject.add_argument("--reason", help="操作原因（最多 255 字符）")
    workflow_cancel = workflow_sub.add_parser("cancel")
    workflow_cancel.add_argument("workflow_id")
    workflow_cancel.add_argument("--reason", help="操作原因（最多 255 字符）")

    notification = sub.add_parser("notification", help="通知查询和已读操作")
    notification_sub = notification.add_subparsers(dest="notification_command")
    notification_list = notification_sub.add_parser("list", help="列出通知")
    notification_list.add_argument("--unread-only", action="store_true")
    notification_list.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    notification_read = notification_sub.add_parser("read", help="标记单条通知已读")
    notification_read.add_argument("notification_id")
    notification_sub.add_parser("read-all", help="标记全部通知已读")

    webhook = sub.add_parser("webhook", help="Webhook 管理操作")
    webhook_sub = webhook.add_subparsers(dest="webhook_command", required=True)
    webhook_sub.add_parser("list", help="列出 Webhook 端点")
    webhook_deliveries = webhook_sub.add_parser("deliveries", help="列出投递记录")
    webhook_deliveries.add_argument("--endpoint-id")
    webhook_deliveries.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    webhook_test = webhook_sub.add_parser("test", help="排队单端点测试通知")
    webhook_test.add_argument("endpoint_id")
    webhook_retry = webhook_sub.add_parser("retry", help="重试死信投递")
    webhook_retry.add_argument("delivery_id")
    sub.add_parser("backup", help="备份只读查询")
    sub.add_parser("deployment", help="部署诊断只读查询")

    library = sub.add_parser("library", help="媒体库只读查询")
    library_sub = library.add_subparsers(dest="library_command", required=True)
    library_list = library_sub.add_parser("list")
    library_list.add_argument("--cursor", type=int, default=0)
    library_list.add_argument("--limit", type=int, default=50)
    library_show = library_sub.add_parser("show")
    library_show.add_argument("library_id")

    media = sub.add_parser("media", help="媒体条目只读查询")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    media_list = media_sub.add_parser("list")
    media_list.add_argument("--library")
    media_list.add_argument("--cursor", type=int, default=0)
    media_list.add_argument("--limit", type=int, default=50)
    media_show = media_sub.add_parser("show")
    media_show.add_argument("media_id")

    audit = sub.add_parser("audit", help="审计记录只读查询")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser("list")
    audit_list.add_argument("--actor")
    audit_list.add_argument("--event-code")
    audit_list.add_argument("--since")
    audit_list.add_argument("--cursor", type=int, default=0)
    audit_list.add_argument("--limit", type=int, default=50)
    audit_show = audit_sub.add_parser("show")
    audit_show.add_argument("audit_id")

    organize = sub.add_parser("organize", help="整理计划查询与本地预览")
    organize_sub = organize.add_subparsers(dest="organize_command", required=True)
    plan = organize_sub.add_parser("plan", help="基于最新完整扫描生成本地整理计划")
    plan.add_argument("--library", dest="library_id", required=True)
    plan.add_argument("--path-id", help="仅规划指定源目录及其子目录")
    plans = organize_sub.add_parser("plans")
    plans.add_argument("--status", choices=("needs_review", "planned", "invalidated", "ignored"))
    plans.add_argument("--cursor", type=int, default=0)
    plans.add_argument("--limit", type=int, default=50)
    plan_show = organize_sub.add_parser("show")
    plan_show.add_argument("plan_id")
    apply = organize_sub.add_parser("apply")
    apply.add_argument("plan_id")
    apply.add_argument("--digest", required=True)
    apply.add_argument("--confirm", action="store_true")
    apply.add_argument("--idempotency-key")

    strm = sub.add_parser("strm", help="STRM 查询与受保护同步")
    strm_sub = strm.add_subparsers(dest="strm_command", required=True)
    strm_status = strm_sub.add_parser("status", help="查看 STRM 能力或媒体库清单摘要")
    strm_status.add_argument("--library")
    generate = strm_sub.add_parser("generate", help="请求受保护的 STRM 全量生成")
    generate.add_argument("--library", dest="library_id", required=True)
    generate.add_argument("--full", action="store_true", required=True)
    generate.add_argument("--workflow-id")
    sync = strm_sub.add_parser("sync", help="请求受保护的 STRM 增量同步")
    sync.add_argument("--library", dest="library_id", required=True)
    sync.add_argument("--workflow-id")
    cleanup_plan = strm_sub.add_parser("cleanup-plan", help="生成只读 STRM 清理计划")
    cleanup_plan.add_argument("--library", dest="library_id", required=True)
    return parser


def _render(payload: dict[str, Any], *, output: str, quiet: bool) -> None:
    if output == "jsonl" and payload.get("ok") and isinstance(payload.get("data"), list):
        for item in payload["data"]:
            print(json.dumps({**payload, "data": item}, ensure_ascii=False, sort_keys=True))
        return
    if output == "json" or output == "jsonl" or quiet:
        if quiet and payload.get("ok"):
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("id", "task_id", "workflow_id"):
                    if data.get(key):
                        print(data[key])
                        return
            print("ok")
            return
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if payload.get("ok"):
        print(json.dumps(payload.get("data"), ensure_ascii=False, indent=2))
    else:
        error = payload.get("error", {})
        print(f"失败 [{error.get('code', 'api_error')}]：{error.get('message_zh', '请求失败')}", file=sys.stderr)


def _wait_for_task(
    client: ApiClient, task_id: str, timeout_seconds: float
) -> tuple[dict[str, Any], int]:
    if timeout_seconds <= 0:
        raise CliFailure("等待超时无效", code=EXIT_USAGE, error_code="invalid_timeout")
    deadline = time.monotonic() + timeout_seconds
    terminal = {"accepted", "failed", "needs_auth", "uncertain"}
    while True:
        body = client.get(f"/api/v1/tasks/{task_id}")
        data = _data_for("/api/v1/tasks", body)
        if isinstance(data, dict) and data.get("state") in terminal:
            return envelope(data=data, request_id=_request_id(body)), EXIT_OK
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (
                envelope(
                    data=data,
                    request_id=_request_id(body),
                    warnings=["等待已超时，服务端任务仍会继续运行"],
                ),
                EXIT_TIMEOUT,
            )
        time.sleep(min(1.0, remaining))


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        payload, code = _command(args, config_path(args.config))
    except CliFailure as error:
        payload, code = error_envelope(error), error.code
    _render(payload, output=args.output if "args" in locals() else "human", quiet=getattr(args, "quiet", False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
