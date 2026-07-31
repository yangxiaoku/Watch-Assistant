# REQ-009 运行中整理操作取消证据

## 状态语义

- `planned` 操作仍由取消 API 直接转为 `cancelled`。
- `organizing` 操作不伪造远端撤回，只持久化 `cancel_requested=true`，并保持运行中状态
  直到 executor 到达下一个 transport 边界。
- 远端写入尚未开始时，executor 将操作完成为 `cancelled`；写入已经开始时，executor
  将结果保持为 `uncertain`，要求后续只读核对。
- 重复提交同一版本的运行中取消请求是幂等的；终态 `uncertain` 仍拒绝普通取消。

## 实现边界

- `organization_operations.cancel_requested` 通过幂等迁移 `051_organization_cancel_requested`
  持久化。
- 正式组织操作 API、操作服务和 executor 共用该状态；没有 CLI 或 Web 专用绕过路径。
- executor 只在 transport 调用前后检查持久请求，不能中断一个已经发出的第三方调用。
- 取消请求不会清除 lease、伪造成功，也不会生成 dirty 事件。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_migrations.py \
  tests/unit/test_organization_operations.py \
  tests/unit/test_organization_executor.py \
  tests/integration/test_organization_operations_api.py \
  tests/contracts/test_event_catalog.py \
  tests/contracts/test_api_error_catalog.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/models.py src/watch_assistant/migrations.py \
  src/watch_assistant/services/organization_operations.py \
  src/watch_assistant/services/organization_executor.py \
  src/watch_assistant/api/organization_operation.py \
  src/watch_assistant/schemas.py src/watch_assistant/services/event_catalog.py \
  tests/unit/test_migrations.py tests/unit/test_organization_operations.py \
  tests/unit/test_organization_executor.py \
  tests/integration/test_organization_operations_api.py

git diff --check
```

结果：专项测试 `56 passed`，Ruff 通过，`git diff --check` 通过。

本证据不包含 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链；真实 115
写入和生产整理仍受现有功能开关、计划确认、receipt-before-verify 与 uncertain 门禁控制。
