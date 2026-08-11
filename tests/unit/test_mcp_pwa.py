import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import PwaDevice
from watch_assistant.schemas import PwaDeviceRegisterRequest
from watch_assistant.security import AuthContext
from watch_assistant.services.mcp import McpService
from watch_assistant.services.pwa_devices import PwaDeviceError, PwaDeviceService


def _context(*scopes: str, library_ids=frozenset()) -> AuthContext:
    return AuthContext(
        identity="agent:one",
        via_bearer=True,
        token_kind="agent",
        agent_token_id="agent_one",
        scopes=frozenset(scopes),
        library_ids=frozenset(library_ids),
    )


class _Tasks:
    async def list_recent(self, *, limit=50, offset=0):
        return [SimpleNamespace(model_dump=lambda **_: {"id": "task_one"})][offset : offset + limit]

    async def get(self, _task_id):
        return SimpleNamespace(model_dump=lambda **_: {"id": "task_one"})


class _Notifications:
    async def list(self, **_):
        return SimpleNamespace(items=[], unread_count=0)


class _OrganizationPlans:
    def __init__(self):
        self.library_ids = None

    async def plan_library_id(self, _plan_id):
        return "library_one"

    async def get_plan(self, _plan_id):
        return SimpleNamespace(
            to_public_dict=lambda: {"plan_id": "plan_one", "plan_hash": "digest_one"}
        )

    async def list_plans(self, *, cursor=0, limit=50, library_ids=None):
        self.library_ids = library_ids
        item = SimpleNamespace(
            to_public_dict=lambda: {"plan_id": "plan_one", "status": "planned"}
        )
        return ([item] if cursor == 0 else []), None


class _OrganizationOperations:
    def __init__(self):
        self.get_calls = 0

    async def plan_digest(self, _plan_id):
        return "digest_one"

    async def create(self, plan_id, *, idempotency_key, expected_plan_revision):
        return SimpleNamespace(
            model_dump=lambda **_: {
                "operation_id": "op_one",
                "plan_id": plan_id,
                "idempotency_key": idempotency_key,
                "revision": expected_plan_revision,
            }
        )

    async def get(self, _operation_id):
        self.get_calls += 1
        return SimpleNamespace(
            model_dump=lambda **_: {"operation_id": "op_one", "status": "planned"}
        )


class _Workflows:
    async def list(self, *, page=1, page_size=50):
        item = SimpleNamespace(
            model_dump=lambda **_: {"id": "wf_one", "status": "in_progress"}
        )
        return SimpleNamespace(items=[item] if page == 1 else [], total=1)

    async def get(self, _workflow_id):
        return SimpleNamespace(
            model_dump=lambda **_: {"id": "wf_one", "status": "in_progress"}
        )


class _ScopedSession:
    def __init__(self, library_id):
        self.library_id = library_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalar(self, _query):
        return self.library_id


class _ScopedSessionFactory:
    def __init__(self, library_id):
        self.library_id = library_id

    def __call__(self):
        return _ScopedSession(self.library_id)


@pytest.mark.asyncio
async def test_mcp_resources_are_allowlisted_and_scope_checked():
    service = McpService(task_service=_Tasks(), notification_service=_Notifications())
    resources = await service.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        context=_context("system:read"),
    )
    names = {item["uri"] for item in resources["result"]["data"]["resources"]}
    assert names == {"watch://system/status", "watch://agent/me"}

    denied = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tasks.list", "arguments": {}},
        },
        context=_context("system:read"),
    )
    assert denied["error"]["data"]["error_code"] == "missing_scope"
    assert denied["error"]["data"]["missing_scopes"] == ["task:read"]
    assert denied["error"]["data"]["title_zh"] == "Agent 权限不足"
    assert denied["error"]["data"]["message_zh"]
    assert denied["error"]["data"]["suggestion_zh"]


@pytest.mark.asyncio
async def test_mcp_task_tool_uses_existing_service():
    service = McpService(task_service=_Tasks(), notification_service=_Notifications())
    response = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "task.get", "arguments": {"task_id": "task_one"}},
        },
        context=_context("task:read"),
    )
    assert response["result"]["data"]["content"][0]["type"] == "text"
    assert "task_one" in response["result"]["data"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_list_resources_exposes_cursor_pagination():
    class Tasks:
        async def list_recent(self, *, limit=50, offset=0):
            def item(i):
                return SimpleNamespace(model_dump=lambda **_: {"id": f"task_{i}"})

            items = [item(i) for i in range(3)]
            return items[offset : offset + limit]

    service = McpService(task_service=Tasks(), notification_service=_Notifications())
    response = await service.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "watch://tasks?limit=2&cursor=0"}},
        context=_context("task:read"),
    )
    data = response["result"]["data"]
    payload = json.loads(data["contents"][0]["text"])
    assert [item["id"] for item in payload["items"]] == ["task_0", "task_1"]
    assert payload["page"]["next_cursor"] == 2


@pytest.mark.asyncio
async def test_mcp_organization_tools_require_scope_library_digest_and_confirmation():
    plans = _OrganizationPlans()
    operations = _OrganizationOperations()
    service = McpService(
        task_service=_Tasks(),
        notification_service=_Notifications(),
        organization_plan_service=plans,
        organization_operation_service=operations,
        workflow_service=_Workflows(),
        library_session_factory=_ScopedSessionFactory("library_one"),
    )
    context = _context(
        "organize:plan", "organize:execute", library_ids={"library_one"}
    )
    listed = await service.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, context=context
    )
    names = {tool["name"] for tool in listed["result"]["data"]["tools"]}
    assert {
        "organization.plan.get",
        "organization.plan.list",
        "organization.plan.apply",
        "organization.operation.get",
    } <= names

    shown = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "organization.plan.get",
                "arguments": {"plan_id": "plan_one"},
            },
        },
        context=context,
    )
    assert "plan_one" in shown["result"]["data"]["content"][0]["text"]

    denied = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "organization.plan.apply",
                "arguments": {
                    "plan_id": "plan_one",
                    "digest": "wrong",
                    "confirm": True,
                    "expected_revision": 1,
                    "idempotency_key": "idem_one",
                },
            },
        },
        context=context,
    )
    assert denied["error"]["data"]["error_code"] == "plan_digest_mismatch"

    applied = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "organization.plan.apply",
                "arguments": {
                    "plan_id": "plan_one",
                    "digest": "digest_one",
                    "confirm": True,
                    "expected_revision": 1,
                    "idempotency_key": "idem_one",
                },
            },
        },
        context=context,
    )
    assert "op_one" in applied["result"]["data"]["content"][0]["text"]

    listed_plans = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "resources/read",
            "params": {"uri": "watch://organization-plans"},
        },
        context=context,
    )
    assert "plan_one" in listed_plans["result"]["data"]["contents"][0]["text"]
    assert plans.library_ids == frozenset({"library_one"})

    listed_tool = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "organization.plan.list",
                "arguments": {},
            },
        },
        context=context,
    )
    assert "plan_one" in listed_tool["result"]["data"]["content"][0]["text"]
    assert plans.library_ids == frozenset({"library_one"})

    operation = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "organization.operation.get",
                "arguments": {"operation_id": "op_one"},
            },
        },
        context=context,
    )
    assert "op_one" in operation["result"]["data"]["content"][0]["text"]

    denied_service = McpService(
        task_service=_Tasks(),
        notification_service=_Notifications(),
        organization_plan_service=plans,
        organization_operation_service=operations,
        library_session_factory=_ScopedSessionFactory("library_other"),
    )
    denied_operation = await denied_service.handle(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "organization.operation.get",
                "arguments": {"operation_id": "op_one"},
            },
        },
        context=context,
    )
    assert denied_operation["error"]["data"]["error_code"] == "resource_forbidden"
    assert operations.get_calls == 1

    workflows = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "workflow.list", "arguments": {"limit": 1}},
        },
        context=_context("task:read"),
    )
    assert "wf_one" in workflows["result"]["data"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_pwa_device_is_encrypted_scoped_and_revocable(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'pwa.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    service = PwaDeviceService(database.session_factory, crypto)
    subscription = {
        "endpoint": "https://push.example.test/device",
        "expirationTime": None,
        "keys": {"p256dh": "public-key", "auth": "auth-key"},
    }
    registered = await service.register(
        "session:owner", PwaDeviceRegisterRequest(name="phone", subscription=subscription)
    )
    assert registered.status == "active"
    async with database.session_factory() as session:
        stored = await session.scalar(select(PwaDevice))
    assert stored is not None
    assert "push.example.test" not in stored.subscription_encrypted
    with pytest.raises(PwaDeviceError) as error:
        await service.revoke("session:other", registered.id)
    assert error.value.code == "pwa_device_not_found"
    revoked = await service.revoke("session:owner", registered.id)
    assert revoked.status == "revoked"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_mcp_library_scoped_token_cannot_read_unscopable_global_entities():
    """library 受限 token 读取任务/通知/工作流(非库维度实体)必须 fail-closed。"""
    service = McpService(task_service=_Tasks(), notification_service=_Notifications())
    context = _context("task:read", library_ids={"library_one"})

    for name, args in (
        ("tasks.list", {}),
        ("task.get", {"task_id": "task_one"}),
        ("notifications.unread", {}),
    ):
        response = await service.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
            context=context,
        )
        assert response["error"]["data"]["error_code"] == "resource_forbidden", name
        assert response["error"]["data"]["title_zh"] == "资源范围不允许", name

    # 资源 URI 路径同样拒绝
    for uri in ("watch://tasks", "watch://notifications/unread", "watch://workflows"):
        response = await service.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}},
            context=context,
        )
        assert response["error"]["data"]["error_code"] == "resource_forbidden", uri

    # 未受限 token 不受影响
    ok = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "tasks.list", "arguments": {}},
        },
        context=_context("task:read"),
    )
    assert "task_one" in ok["result"]["data"]["content"][0]["text"]


class _OrmLikeTask:
    """模拟 SQLAlchemy ORM 对象:无 model_dump,不可直接 json.dumps。"""

    def __init__(self, task_id: str, state: str = "downloading"):
        from datetime import UTC, datetime

        self.id = task_id
        self.state = state
        self.action = "offline_download"
        self.attempts = 0
        self.remote_ref = None
        self.error_code = None
        self.error_message = None
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.submitted_at = None
        self.workflow_id = None
        self.resource_id = None
        self.target_directory_id = None


class _OrmTasks:
    async def list_recent(self, *, limit=50, offset=0):
        return [_OrmLikeTask("task_orm")][offset : offset + limit]

    async def get(self, task_id: str):
        del task_id
        return _OrmLikeTask("task_orm")


@pytest.mark.asyncio
async def test_mcp_tasks_never_serialize_raw_orm_objects():
    """修复前:TaskService.list_recent()/get() 返回 SQLAlchemy ORM 对象,
    _model_dump 原样返回 → json.dumps 抛 TypeError → 裸 500。修复后:
    MCP 层返回稳定的公开任务视图。"""
    service = McpService(task_service=_OrmTasks(), notification_service=_Notifications())
    response = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "task.get", "arguments": {"task_id": "task_orm"}},
        },
        context=_context("task:read"),
    )
    assert "error" not in response, response
    text = response["result"]["data"]["content"][0]["text"]
    assert "task_orm" in text
    json.loads(text)  # 必须是可解析的 JSON,而非 ORM 对象


@pytest.mark.asyncio
async def test_mcp_organization_apply_rejects_non_ascii_digest():
    """digest 非 ASCII 时 _organization_apply_arguments 必须拒绝为
    invalid_request,而非让 hmac.compare_digest 抛 TypeError 崩溃。"""
    plans = _OrganizationPlans()
    operations = _OrganizationOperations()
    service = McpService(
        task_service=_Tasks(),
        notification_service=_Notifications(),
        organization_plan_service=plans,
        organization_operation_service=operations,
        workflow_service=_Workflows(),
        library_session_factory=_ScopedSessionFactory("library_one"),
    )
    context = _context(
        "organize:plan", "organize:execute", library_ids={"library_one"}
    )
    response = await service.handle(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "organization.plan.apply",
                "arguments": {
                    "plan_id": "plan_one",
                    "digest": "中文摘要-" + "a" * 10,  # 非 ASCII
                    "confirm": True,
                    "expected_revision": 1,
                    "idempotency_key": "key-1",
                },
            },
        },
        context=context,
    )
    assert "error" in response
    assert response["error"]["data"]["error_code"] == "invalid_request"
