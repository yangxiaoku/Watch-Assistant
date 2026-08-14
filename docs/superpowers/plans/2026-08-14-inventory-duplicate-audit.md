# 库存重复检测报告（只读）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 盘点整理归档目录，输出「完全重复」与「同片多版本」两类只读报告；本期零 115 写操作，Web 展示并预留置灰的删除/洗版入口。

**Architecture:** 复用 storage_governance（内容重复判定）与 media_parser（多版本清晰度语义）作为判定核心，新增只读服务 inventory_audit 输入扫描快照输出报告；新增只读 API 与 Web 页。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (async) / SQLite / pydantic / 现有 LibraryIndexService、P115ReadOnlyDirectoryGateway、storage_governance、media_parser。

## Global Constraints

- 使用 `/Users/apple/Watch-Assistant/.venv/bin/python`。
- **本期零写操作**：只读网关、只读扫描，不产生任何 115 移动/删除/重命名。
- 删除/洗版入口本期置灰（disabled），不做真实动作；未来接入须走「预览计划→人工确认→幂等→审计」门禁，永久删除默认关闭。
- 用户可见文案中文，机器码英文 snake_case。
- 复用现有判定逻辑，不重造轮子。
- 遵循 AGENTS.md。

---

### Task 1: InventoryAudit 报告数据结构 + 判定纯函数

**Files:**
- Create: `src/watch_assistant/services/inventory_audit.py`
- Test: `tests/unit/test_inventory_audit.py`

**Interfaces:**
- Produces:
  - `@dataclass InventoryAuditGroup`：`group_id\`, `kind\`("exact_duplicate"|"multi_version")，`items\`（每项 object_id/path/name/size/清晰度标签），`reclaimable_bytes\`。
  - `@dataclass InventoryAuditReport`：`groups\`, `duplicate_count\`, `reclaimable_bytes\`。
  - `def build_audit_report(entries: list[dict]) -> InventoryAuditReport`（纯函数；输入为扫描快照条目，含 object_id/path/name/size/清晰度标签、内容指纹）。
  - 复用：内容指纹复用 storage_governance 的判重键；多版本复用 media_parser 解析出「剧集/季集」+ 清晰度标签。

- [ ] **Step 1: 写失败测试**：给定两个同内容指纹条目 + 两个同集不同清晰度条目，断言分成两类、reclaimable_bytes 只算重复项。
- [ ] **Step 2: 确认失败**（ModuleNotFoundError）
- [ ] **Step 3: 实现 build_audit_report**（分桶：指纹相同→exact_duplicate；指纹不同但解析出的剧集/季集相同且清晰度不同→multi_version）。
- [ ] **Step 4: 通过**
- [ ] **Step 5: 提交**
```bash
git add src/watch_assistant/services/inventory_audit.py tests/unit/test_inventory_audit.py
git commit -m "feat: 库存重复检测报告数据结构与判定"
```

---

### Task 2: 扫描快照接入（只读）

**Files:**
- Create: `src/watch_assistant/services/inventory_audit_service.py`
- Test: `tests/unit/test_inventory_audit_service.py`

**Interfaces:**
- Produces: `class InventoryAuditService(session_factory, gateway_factory)`：`async run_audit(target_root_id) -> InventoryAuditReport`。
- 复用 LibraryIndexService 扫描快照 + P115ReadOnlyDirectoryGateway 只读网关；目标根取自 `app.state.organization_target_root_id`。

- [ ] Step 1-2: 失败测试（mock gateway 返回固定目录条目）。
- [ ] Step 3: 实现（调用只读网关拉条目 → 转 dict → build_audit_report）。
- [ ] Step 4: 通过。
- [ ] Step 5: 提交。

---

### Task 3: 只读 API

**Files:**
- Create: `src/watch_assistant/api/inventory.py`
- Modify: `src/watch_assistant/app.py`（注册路由、装配 service）
- Modify: `src/watch_assistant/security.py`（_required_scope 登记 `/api/v1/inventory` → library:read）
- Test: `tests/integration/test_inventory_audit_api.py`

**Interfaces:**
- Produces: `GET /api/v1/inventory/audit` → InventoryAuditReport（无货时返回空报告 + 汇总 0）；目标根未配置/未 scope_verified 时 fail-closed 提示「整理归档目录未配置」（状态码 503）。

- [ ] Step 1-5: TDD 写路由，验证 scope（library:read）、未配置 fail-closed、报告字段。

---

### Task 4: Web「库存体检」页（预留置灰入口）

**Files:**
- Modify: `frontend/src/views/`（新增 InventoryAuditView.vue 或并入 LibraryView）
- Modify: `frontend/src/api.ts`、`frontend/src/types.ts`、`frontend/src/nav.ts` / `router.ts`

- [ ] 列表按剧分组，两类分栏（完全重复 / 多版本），每项「删除/洗版」按钮 `disabled` + tooltip「即将上线」。
- [ ] 前端构建 `npm --prefix frontend run build` 通过。

---

### Task 5: 全量回归
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m pytest tests/unit tests/integration -q`
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m pytest tests/contracts -q`
- [ ] `/Users/apple/Watch-Assistant/.venv/bin/python -m ruff check src tests`
- [ ] `npm --prefix frontend test -- --run`
- [ ] 修复 + commit

---

## Self-Review 清单
- 覆盖 spec 全部：报告两类、只读、复用判定、fail-closed、置灰入口。
- 无占位符；类型一致（InventoryAuditReport/InventoryAuditGroup 跨任务一致）。
- 安全：零写；删除/洗版仅 UI 置灰，不接线真实动作。
