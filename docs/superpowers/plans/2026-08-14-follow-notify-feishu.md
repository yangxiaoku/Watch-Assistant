# 追更通知闭环（飞书）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 订阅命中新集后，主动推送飞书群机器人消息（含跳回 Watch Assistant 的链接）；真正入 115 后再补一条「已入库」消息。

**Architecture:** 新增独立通知渠道层（models.NotifyChannel + services/notify_channels + notify_dispatcher），复用现有 SecretCrypto 加密落库与 emit_event 事件桥；飞书 webhook 客户端 trust_env=False。触发点挂在两个已有事件上：subscription.resources_observed（找到）与 task.availability_verified（已入库）。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (async) / SQLite / pydantic / httpx / 现有 SecretCrypto(Fernet) 与 emit_event 基础设施。

## Global Constraints

- 只改 src/watch_assistant 与 tests；使用 `/Users/apple/Watch-Assistant/.venv/bin/python`，不用系统 Python、不 uv sync。
- 用户可见文案用中文；稳定机器码用英文 snake_case。
- 凭据（飞书 webhook URL）用 SecretCrypto 加密落库，API/UI 绝不回显明文，只回显前缀 + 尾 4 位。
- 新增事件必须先在 services/event_catalog.py 登记（allowed_fields 白名单），未知字段会被丢弃。
- 出网 httpx 客户端一律 trust_env=False。
- 遵循 AGENTS.md：不做破坏性 git 操作，最小改动，补单测与事件映射。
- 本计划不涉及 115 写操作、不涉及删除/洗版。

---

### Task 1: NotifyChannel 模型与迁移

**Files:**
- Modify: `src/watch_assistant/models.py`（新增类）
- Modify: `src/watch_assistant/migrations.py`（新增 Migration 并注册）
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Produces: 模型类 `NotifyChannel`，表名 `notify_channels`。

- [ ] **Step 1: 写失败测试**
```python
def test_notify_channel_table_exists():
    from watch_assistant.models import NotifyChannel
    assert NotifyChannel.__tablename__ == "notify_channels"
```

- [ ] **Step 2: 运行确认失败**
Run: `/Users/apple/Watch-Assistant/.venv/bin/python -m pytest tests/unit/test_models.py::test_notify_channel_table_exists -q`
Expected: FAIL（ImportError）

- [ ] **Step 3: 新增模型**（在 WebhookEndpoint 后）
```python
class NotifyChannel(Base):
    """External push channel config (e.g. Feishu bot); webhook URL is encrypted."""
    __tablename__ = "notify_channels"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # "feishu"
    webhook_url_encrypted: Mapped[str] = mapped_column(Text)
    webhook_url_prefix: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
```

- [ ] **Step 4: 注册迁移**
```python
def _create_notify_channels_table(connection: Connection) -> None:
    from watch_assistant.models import NotifyChannel
    NotifyChannel.__table__.create(connection, checkfirst=True)
# MIGRATIONS 末尾追加（编号取下一个未用整数，先 read 确认）
Migration("080_notify_channels", _create_notify_channels_table),
```

- [ ] **Step 5: 运行通过** `pytest tests/unit/test_models.py -q`
- [ ] **Step 6: 提交**
```bash
git add src/watch_assistant/models.py src/watch_assistant/migrations.py tests/unit/test_models.py
git commit -m "feat: 新增 NotifyChannel 通知渠道模型与迁移"
```

---

### Task 2: 飞书渠道适配器

**Files:**
- Create: `src/watch_assistant/services/notify_channels/__init__.py`
- Create: `src/watch_assistant/services/notify_channels/feishu.py`
- Test: `tests/unit/test_notify_feishu.py`

**Interfaces:**
- Produces: `build_feishu_payload(title, text, link_url) -> dict`；`class FeishuChannel(webhook_url, *, timeout=10.0)` 含 `async send(*, title, text, link_url=None) -> bool` 与 `async aclose()`。

- [ ] **Step 1: 写失败测试**（test_build_feishu_payload 断言 msg_type=="interactive" 且卡片含标题）
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（httpx.AsyncClient(trust_env=False)；send 内 post + raise_for_status，HTTPError 返回 False）
- [ ] **Step 4: 用 httpx.MockTransport 断言 POST 到 open.feishu.cn 且返回 True**
- [ ] **Step 5: 提交**
```bash
git add src/watch_assistant/services/notify_channels/ tests/unit/test_notify_feishu.py
git commit -m "feat: 新增飞书通知渠道适配器"
```

---

### Task 3: 通知渠道 CRUD 服务（加密 + 脱敏回显）

**Files:**
- Create: `src/watch_assistant/services/notify_channels_service.py`
- Test: `tests/unit/test_notify_channels_service.py`

**Interfaces:**
- Produces: `class NotifyChannelService(session_factory, crypto)`：`async create(name, webhook_url, *, kind="feishu") -> resp`、`async list() -> list[resp]`、`async set_enabled(id, enabled) -> resp`、`async delete(id) -> None`；响应只含 webhook_url_prefix（前缀+…+尾4位），绝不含完整 URL。

- [ ] Step 1-2: 写 test_create_encrypts_and_masks_url（断言尾4位在 prefix，完整串不回显），确认失败。
- [ ] Step 3: 实现（crypto.encrypt 落库；mask 计算 prefix）。
- [ ] Step 4: 通过。
- [ ] Step 5: 提交。

---

### Task 4: 通知分发器（两条触发）

**Files:**
- Create: `src/watch_assistant/services/notify_dispatcher.py`
- Modify: `src/watch_assistant/services/event_catalog.py`（登记 notify.delivered / notify.delivery_failed）
- Modify: `src/watch_assistant/services/subscriptions.py`（通知 A）
- Modify: `src/watch_assistant/worker.py`（通知 B）
- Test: `tests/unit/test_notify_dispatcher.py`

**Interfaces:**
- Produces: `class NotifyDispatcher(session_factory, crypto, event_logger, base_url="http://192.168.6.236:8115")`：`async dispatch_notify(*, title, text, link_path) -> int`；`async notify_resource_found(subscription_id) -> None`；`async notify_task_available(task_id) -> None`。

- [ ] Step 1-2: 失败测试。
- [ ] Step 3: 实现（dispatch 查启用渠道→解密→send→失败 emit 事件；notify_resource_found 反查订阅→tmdb→title；notify_task_available 反查 task→resource→订阅）。
- [ ] Step 4: subscriptions.py 在 emit("subscription.resources_observed") 后调用 dispatcher.notify_resource_found（若注入）；worker.py 在 emit("task.availability_verified") 后调用 notify_task_available（若注入）。
- [ ] Step 5: 通过 + 提交。

---

### Task 5: 设置 API + 前端（可选最小）

- Modify api/settings.py 或新增 api/notify_channels.py：`GET/POST/DELETE /api/v1/notify-channels`，scope settings:read/write（在 security.py _required_scope 登记前缀）。
- Modify app.py 装配 service/dispatcher 到 app.state 并注册路由。
- 前端 SettingsView.vue 增加「通知渠道」区块（可选，后端 API 优先）。

---

### Task 6: 全量回归
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m pytest tests/unit tests/integration -q`
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m pytest tests/contracts -q`
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m ruff check src tests`
- [ ] 修复失败 + commit
