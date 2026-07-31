# REQ-009 workflow Agent 筛选证据

日期：2026-07-31

## 完成范围

- 认证 Bearer Agent 创建 workflow 时，服务端持久化 `actor_type=agent` 和 Agent 记录 ID。
- workflow 响应只返回非敏感 `agent_id`；Web Session 不保存原始 Session ID 或 Cookie。
- API、`watchctl workflow list --agent-id` 和 MCP `workflow.list` 共用同一服务端 Agent 筛选。
- 迁移 055 对旧 `workflows` 表幂等添加 `actor_type`、`actor_id` 和组合索引。

## 验证

```text
.venv/bin/python -m pytest -q \
  tests/integration/test_workflows_api.py \
  tests/unit/test_mcp_pwa.py \
  tests/unit/test_watchctl.py \
  tests/unit/test_migrations.py
```

结果：44 passed，1 个既有 SQLite 弃用警告。

```text
.venv/bin/ruff check src tests scripts
git diff --check
```

结果：Ruff 和格式检查通过。

本次验证使用合成 Agent 夹具，没有输出真实 Token、Cookie、磁力、pickcode 或播放直链。

## 未覆盖

REQ-009 的完整业务事件矩阵、生产 115/STRM、外部通知渠道和发布基线合入仍按阻断记录处理。
