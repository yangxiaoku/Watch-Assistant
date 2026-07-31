"""Small, allow-listed MCP JSON-RPC surface backed by existing services."""

from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from watch_assistant.library_models import LibraryScanRun, MediaLibrary
from watch_assistant.security import AuthContext
from watch_assistant.services.api_errors import build_error_payload
from watch_assistant.services.observability import EventLogger, emit_event


class McpError(ValueError):
    def __init__(self, code: str, *, missing_scopes: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.missing_scopes = missing_scopes or []


class McpService:
    def __init__(
        self,
        *,
        task_service: Any,
        notification_service: Any,
        organization_plan_service: Any | None = None,
        organization_operation_service: Any | None = None,
        workflow_service: Any | None = None,
        library_session_factory: Any | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._tasks = task_service
        self._notifications = notification_service
        self._organization_plans = organization_plan_service
        self._organization_operations = organization_operation_service
        self._workflows = workflow_service
        self._library_session_factory = library_session_factory
        self._event_logger = event_logger

    async def handle(
        self,
        request: dict[str, Any],
        *,
        context: AuthContext,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        method = request.get("method")
        request_number = request.get("id")
        if not isinstance(method, str) or len(method) > 80:
            return _error(request_number, -32600, "invalid_request", request_id, correlation_id)
        try:
            result = await self._dispatch(method, request.get("params"), context)
            await emit_event(
                self._event_logger,
                "mcp.call",
                fields={"method": method, "status": "succeeded"},
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type="agent",
                actor_id=context.agent_token_id,
            )
            if request_number is None:
                return {}
            return {"jsonrpc": "2.0", "id": request_number, "result": _envelope(result, request_id, correlation_id)}
        except McpError as exc:
            await emit_event(
                self._event_logger,
                "mcp.call",
                fields={"method": method, "status": "failed"},
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type="agent",
                actor_id=context.agent_token_id,
            )
            return _error(request_number, -32003, exc.code, request_id, correlation_id, exc.missing_scopes)

    async def _dispatch(self, method: str, params: Any, context: AuthContext) -> dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {"listChanged": False}, "tools": {"listChanged": False}},
                "serverInfo": {"name": "watch-assistant", "version": "v1"},
            }
        if method == "ping":
            return {}
        if method == "resources/list":
            return {"resources": _resources(context)}
        if method == "resources/read":
            uri = params.get("uri")
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": _json_text(await self._resource(uri, context)),
                    }
                ]
            }
        if method == "tools/list":
            return {"tools": _tools(context)}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            return {"content": [{"type": "text", "text": _json_text(await self._tool(name, arguments, context))}], "isError": False}
        raise McpError("method_not_found")

    async def _resource(self, uri: Any, context: AuthContext) -> dict[str, Any]:
        if not isinstance(uri, str) or not uri.startswith("watch://"):
            raise McpError("resource_not_found")
        parsed_uri = urlsplit(uri)
        base_uri = urlunsplit(parsed_uri._replace(query="", fragment=""))
        if base_uri == "watch://system/status":
            self._require(context, "system:read")
            return {"schema_version": "v1", "generated_at": _now(), "freshness": "live", "status": "ok"}
        if base_uri == "watch://agent/me":
            self._require(context, "system:read")
            return {"schema_version": "v1", "generated_at": _now(), "freshness": "live", "token_id": context.agent_token_id, "token_name": context.agent_name, "scopes": sorted(context.scopes), "library_ids": sorted(context.library_ids)}
        if base_uri == "watch://tasks":
            self._require(context, "task:read")
            return await self._paged_tasks(uri, context)
        if base_uri == "watch://libraries":
            self._require(context, "library:read")
            return await self._paged_libraries(uri, context)
        if base_uri == "watch://notifications/unread":
            self._require(context, "task:read")
            return await self._paged_notifications(uri)
        if base_uri == "watch://workflows":
            self._require(context, "task:read")
            return await self._paged_workflows(uri)
        if base_uri == "watch://organization-plans":
            self._require(context, "organize:plan")
            return await self._paged_organization_plans(uri)
        raise McpError("resource_not_found")

    async def _paged_tasks(self, uri: str, context: AuthContext) -> dict[str, Any]:
        limit, cursor = _page_from_uri(uri)
        try:
            items = await self._tasks.list_recent(limit=limit + 1, offset=cursor)
        except TypeError:
            # Keep small test doubles and older service adapters compatible.
            items = (await self._tasks.list_recent())[cursor : cursor + limit + 1]
        has_more = len(items) > limit
        return {
            "schema_version": "v1",
            "generated_at": _now(),
            "freshness": "live",
            "items": [_model_dump(item) for item in items[:limit]],
            "page": {"limit": limit, "cursor": cursor, "next_cursor": cursor + limit if has_more else None},
        }

    async def _paged_libraries(self, uri: str, context: AuthContext) -> dict[str, Any]:
        if self._library_session_factory is None:
            raise McpError("mcp_unavailable")
        limit, cursor = _page_from_uri(uri)
        async with self._library_session_factory() as session:
            query = select(MediaLibrary).order_by(MediaLibrary.id)
            if context.via_bearer and context.library_ids:
                query = query.where(MediaLibrary.id.in_(context.library_ids))
            libraries = list((await session.scalars(query.offset(cursor).limit(limit + 1))).all())
            has_more = len(libraries) > limit
            libraries = libraries[:limit]
            latest: dict[str, LibraryScanRun] = {}
            if libraries:
                scans = await session.scalars(
                    select(LibraryScanRun)
                    .where(
                        LibraryScanRun.library_id.in_([library.id for library in libraries]),
                        LibraryScanRun.complete.is_(True),
                        LibraryScanRun.state == "completed",
                    )
                    .order_by(
                        LibraryScanRun.library_id,
                        LibraryScanRun.snapshot_revision.desc(),
                    )
                )
                for scan in scans:
                    latest.setdefault(scan.library_id, scan)
            items = [
                {
                    "library_id": library.id,
                    "name": library.name,
                    "enabled": library.enabled,
                    "scope_verified": library.scope_verified,
                    "revision": library.revision,
                    "latest_scan": _library_scan_dict(latest.get(library.id)),
                }
                for library in libraries
            ]
        return {
            "schema_version": "v1",
            "generated_at": _now(),
            "freshness": "live",
            "items": items,
            "page": {
                "limit": limit,
                "cursor": cursor,
                "next_cursor": cursor + limit if has_more else None,
            },
        }

    async def _paged_notifications(self, uri: str) -> dict[str, Any]:
        limit, cursor = _page_from_uri(uri)
        response = await self._notifications.list(
            unread_only=True, limit=limit + 1, offset=cursor
        )
        items = list(response.items)
        has_more = len(items) > limit
        return {
            "schema_version": "v1",
            "generated_at": _now(),
            "freshness": "live",
            "items": [_model_dump(item) for item in items[:limit]],
            "unread_count": response.unread_count,
            "page": {"limit": limit, "cursor": cursor, "next_cursor": cursor + limit if has_more else None},
        }

    async def _paged_workflows(self, uri: str) -> dict[str, Any]:
        if self._workflows is None:
            raise McpError("mcp_unavailable")
        limit, cursor = _page_from_uri(uri)
        parsed = urlsplit(uri)
        values = parse_qs(parsed.query, keep_blank_values=True)
        agent_id = values.get("agent_id", [None])[0]
        if agent_id is not None and _IDENTIFIER.fullmatch(agent_id) is None:
            raise McpError("invalid_request")
        page_args: dict[str, Any] = {
            "page": (cursor // limit) + 1,
            "page_size": limit,
        }
        if agent_id is not None:
            page_args["agent_id"] = agent_id
        page = await self._workflows.list(**page_args)
        items = list(page.items)
        has_more = cursor + len(items) < page.total
        return {
            "schema_version": "v1",
            "generated_at": _now(),
            "freshness": "live",
            "items": [_model_dump(item) for item in items],
            "page": {
                "limit": limit,
                "cursor": cursor,
                "next_cursor": cursor + limit if has_more else None,
            },
        }

    async def _paged_organization_plans(self, uri: str) -> dict[str, Any]:
        if self._organization_plans is None:
            raise McpError("mcp_unavailable")
        limit, cursor = _page_from_uri(uri)
        try:
            items, next_cursor = await self._organization_plans.list_plans(
                cursor=cursor, limit=limit
            )
        except Exception:  # noqa: BLE001 - keep plan details behind MCP errors
            raise McpError("organization_plan_unavailable") from None
        return {
            "schema_version": "v1",
            "generated_at": _now(),
            "freshness": "live",
            "items": [item.to_public_dict() for item in items],
            "page": {"limit": limit, "cursor": cursor, "next_cursor": next_cursor},
        }

    async def _tool(self, name: Any, arguments: Any, context: AuthContext) -> dict[str, Any]:
        if name == "system.status":
            self._require(context, "system:read")
            return await self._resource("watch://system/status", context)
        if name == "tasks.list":
            self._require(context, "task:read")
            return await self._paged_tasks(
                _paged_uri("watch://tasks", arguments), context
            )
        if name == "library.list":
            self._require(context, "library:read")
            return await self._paged_libraries(
                _paged_uri("watch://libraries", arguments), context
            )
        if name == "notifications.unread":
            self._require(context, "task:read")
            return await self._paged_notifications(
                _paged_uri("watch://notifications/unread", arguments)
            )
        if name == "workflow.list":
            self._require(context, "task:read")
            return await self._paged_workflows(_paged_uri("watch://workflows", arguments))
        if name == "workflow.get":
            self._require(context, "task:read")
            if self._workflows is None:
                raise McpError("mcp_unavailable")
            workflow_id = _required_identifier(arguments, "workflow_id")
            try:
                workflow = await self._workflows.get(workflow_id)
            except Exception:  # noqa: BLE001 - do not expose existence details
                raise McpError("workflow_not_found") from None
            return {
                "schema_version": "v1",
                "generated_at": _now(),
                "freshness": "live",
                "item": _model_dump(workflow),
            }
        if name == "organization.plan.list":
            self._require(context, "organize:plan")
            return await self._paged_organization_plans(
                _paged_uri("watch://organization-plans", arguments)
            )
        if name == "organization.operation.get":
            self._require(context, "organize:execute")
            if self._organization_operations is None:
                raise McpError("mcp_unavailable")
            operation_id = _required_identifier(arguments, "operation_id")
            try:
                operation = await self._organization_operations.get(operation_id)
            except Exception:  # noqa: BLE001 - do not expose existence details
                raise McpError("operation_not_found") from None
            return {
                "schema_version": "v1",
                "generated_at": _now(),
                "freshness": "live",
                "item": _model_dump(operation),
            }
        if name == "task.get":
            self._require(context, "task:read")
            task_id = arguments.get("task_id") if isinstance(arguments, dict) else None
            if not isinstance(task_id, str) or len(task_id) > 64:
                raise McpError("invalid_request")
            task = await self._tasks.get(task_id)
            if task is None:
                raise McpError("task_not_found")
            return {"schema_version": "v1", "generated_at": _now(), "freshness": "live", "item": _model_dump(task)}
        if name == "organization.plan.get":
            self._require(context, "organize:plan")
            plan_id = _required_identifier(arguments, "plan_id")
            service = self._organization_plans
            if service is None:
                raise McpError("mcp_unavailable")
            await self._check_plan_scope(service, plan_id, context)
            try:
                plan = await service.get_plan(plan_id)
            except Exception:  # noqa: BLE001 - business details stay behind MCP errors
                raise McpError("plan_not_found") from None
            return {"schema_version": "v1", "generated_at": _now(), "freshness": "live", "item": plan.to_public_dict()}
        if name == "organization.plan.apply":
            self._require(context, "organize:execute")
            values = _organization_apply_arguments(arguments)
            plan_service = self._organization_plans
            operation_service = self._organization_operations
            if plan_service is None or operation_service is None:
                raise McpError("mcp_unavailable")
            await self._check_plan_scope(plan_service, values["plan_id"], context)
            try:
                expected = await operation_service.plan_digest(values["plan_id"])
            except Exception:  # noqa: BLE001 - business details stay behind MCP errors
                raise McpError("plan_not_found") from None
            if not hmac.compare_digest(expected, values["digest"]):
                raise McpError("plan_digest_mismatch")
            try:
                summary = await operation_service.create(
                    values["plan_id"],
                    idempotency_key=values["idempotency_key"],
                    expected_plan_revision=values["expected_revision"],
                )
            except Exception as error:  # noqa: BLE001 - map to stable MCP codes
                raise McpError(_organization_error_code(error)) from None
            return {"schema_version": "v1", "generated_at": _now(), "freshness": "live", "item": _model_dump(summary)}
        raise McpError("tool_not_found")

    async def _check_plan_scope(self, service: Any, plan_id: str, context: AuthContext) -> None:
        if not context.via_bearer or not context.library_ids:
            return
        try:
            library_id = await service.plan_library_id(plan_id)
        except Exception:  # noqa: BLE001 - do not reveal plan existence
            raise McpError("plan_not_found") from None
        if library_id not in context.library_ids:
            raise McpError("resource_forbidden")

    @staticmethod
    def _require(context: AuthContext, scope: str) -> None:
        if not context.has_scope(scope):
            raise McpError("missing_scope", missing_scopes=[scope])


def _resources(context: AuthContext) -> list[dict[str, Any]]:
    resources = [
        {"uri": "watch://system/status", "name": "系统状态", "mimeType": "application/json"},
        {"uri": "watch://agent/me", "name": "Agent 身份与权限", "mimeType": "application/json"},
    ]
    if context.has_scope("library:read"):
        resources.append(
            {"uri": "watch://libraries", "name": "媒体库", "mimeType": "application/json"}
        )
    if context.has_scope("task:read"):
        resources.extend([
            {"uri": "watch://tasks", "name": "任务摘要", "mimeType": "application/json"},
            {"uri": "watch://notifications/unread", "name": "未读通知", "mimeType": "application/json"},
            {"uri": "watch://workflows", "name": "工作流状态", "mimeType": "application/json"},
        ])
    if context.has_scope("organize:plan"):
        resources.append(
            {"uri": "watch://organization-plans", "name": "整理计划", "mimeType": "application/json"}
        )
    return resources


def _tools(context: AuthContext) -> list[dict[str, Any]]:
    tools = [{"name": "system.status", "description": "读取系统状态", "inputSchema": {"type": "object", "additionalProperties": False}}]
    if context.has_scope("task:read"):
        tools.extend([
            {"name": "tasks.list", "description": "读取任务摘要", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
            {"name": "task.get", "description": "读取单个任务", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string", "maxLength": 64}}, "required": ["task_id"], "additionalProperties": False}},
            {"name": "notifications.unread", "description": "读取未读通知", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
            {"name": "workflow.list", "description": "读取工作流状态", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "integer", "minimum": 0}, "agent_id": {"type": "string", "maxLength": 255}}, "additionalProperties": False}},
            {"name": "workflow.get", "description": "读取工作流详情", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string", "maxLength": 255}}, "required": ["workflow_id"], "additionalProperties": False}},
        ])
    if context.has_scope("organize:plan"):
        tools.append({
            "name": "organization.plan.get",
            "description": "读取整理计划",
            "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string", "maxLength": 64}}, "required": ["plan_id"], "additionalProperties": False},
        })
        tools.append({
            "name": "organization.plan.list",
            "description": "分页读取整理计划",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        })
    if context.has_scope("organize:execute"):
        tools.append({
            "name": "organization.plan.apply",
            "description": "提交已确认整理计划",
            "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string", "maxLength": 64}, "digest": {"type": "string", "minLength": 1, "maxLength": 128}, "confirm": {"type": "boolean"}, "expected_revision": {"type": "integer", "minimum": 1}, "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 255}}, "required": ["plan_id", "digest", "confirm", "expected_revision", "idempotency_key"], "additionalProperties": False},
        })
        tools.append({
            "name": "organization.operation.get",
            "description": "读取整理操作状态",
            "inputSchema": {"type": "object", "properties": {"operation_id": {"type": "string", "maxLength": 255}}, "required": ["operation_id"], "additionalProperties": False},
        })
    return tools


def _envelope(data: dict[str, Any], request_id: str | None, correlation_id: str | None) -> dict[str, Any]:
    return {"ok": True, "schema_version": "v1", "request_id": request_id, "correlation_id": correlation_id, "data": data, "warnings": [], "next_actions": []}


def _error(request_number: Any, code: int, error_code: str, request_id: str | None, correlation_id: str | None, missing_scopes: list[str] | None = None) -> dict[str, Any]:
    if request_number is None:
        return {}
    payload = build_error_payload(
        error_code,
        503 if error_code == "mcp_unavailable" else 400,
        request_id=request_id or "mcp-request",
        correlation_id=correlation_id or "mcp-correlation",
        missing_scopes=missing_scopes,
    )
    error: dict[str, Any] = {
        "code": code,
        "message": payload["title_zh"],
        "data": {
            "error_code": error_code,
            "title_zh": payload["title_zh"],
            "message_zh": payload["message_zh"],
            "suggestion_zh": payload["suggestion_zh"],
            "retryable": payload["retryable"],
            "action": payload["action"],
            "missing_scopes": payload.get("missing_scopes", []),
        },
    }
    return {"jsonrpc": "2.0", "id": request_number, "error": error, "meta": {"request_id": request_id, "correlation_id": correlation_id}}


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _library_scan_dict(scan: LibraryScanRun | None) -> dict[str, Any] | None:
    if scan is None:
        return None
    return {
        "state": scan.state,
        "complete": scan.complete,
        "snapshot_revision": scan.snapshot_revision,
        "pages_read": scan.pages_read,
        "items_seen": scan.items_seen,
        "updated_at": scan.updated_at.isoformat(),
    }


def _page_from_uri(uri: str) -> tuple[int, int]:
    parsed = urlsplit(uri)
    values = parse_qs(parsed.query, keep_blank_values=True)
    try:
        limit = int(values.get("limit", ["50"])[0])
        cursor = int(values.get("cursor", ["0"])[0])
    except (TypeError, ValueError):
        raise McpError("invalid_pagination") from None
    if not 1 <= limit <= 100 or cursor < 0:
        raise McpError("invalid_pagination")
    return limit, cursor


def _paged_uri(uri: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        arguments = {}
    values = {
        key: str(arguments[key])
        for key in ("limit", "cursor", "agent_id")
        if key in arguments
    }
    if not values:
        return uri
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(query=urlencode(values)))


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,255}\Z")


def _required_identifier(arguments: Any, name: str) -> str:
    value = arguments.get(name) if isinstance(arguments, dict) else None
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise McpError("invalid_request")
    return value


def _organization_apply_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments.get("confirm") is not True:
        raise McpError("confirmation_required")
    plan_id = _required_identifier(arguments, "plan_id")
    digest = arguments.get("digest")
    key = arguments.get("idempotency_key")
    revision = arguments.get("expected_revision")
    if (
        not isinstance(digest, str)
        or not 1 <= len(digest) <= 128
        or not isinstance(key, str)
        or not 1 <= len(key) <= 255
        or _IDENTIFIER.fullmatch(key) is None
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise McpError("invalid_request")
    return {"plan_id": plan_id, "digest": digest, "idempotency_key": key, "expected_revision": revision}


def _organization_error_code(error: Exception) -> str:
    code = str(error)
    return code if code in {
        "plan_prerequisites_changed",
        "plan_revision_changed",
        "idempotency_key_conflict",
        "operation_plan_conflict",
        "operation_creation_conflict",
    } else "organization_operation_unavailable"


__all__ = ["McpError", "McpService"]
