# REQ-009 workflow 阶段事件证据

## 实现范围

- `sync_child_stage_in_transaction` 统一在事务提交后发出 `workflow.stage_changed`。
- 资源搜索和内容检测的持久状态写入复用同一事件辅助函数，携带 workflow ID、correlation ID、
  阶段、状态和稳定错误码。
- 通知服务只为完成、失败、跳过、不确定、取消和等待状态生成站内通知，运行中阶段仅写结构化日志。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/integration/test_search_api.py tests/integration/test_inspection_api.py \
  tests/integration/test_notifications_api.py tests/integration/test_workflows_api.py \
  tests/unit/test_notifications.py tests/unit/test_search_service.py \
  tests/contracts/test_event_catalog.py tests/contracts/test_api_error_catalog.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/services/workflows.py src/watch_assistant/services/search.py \
  src/watch_assistant/services/inspection.py tests/integration/test_search_api.py \
  tests/integration/test_inspection_api.py

git diff --check
```

结果：专项测试 `81 passed`，Ruff 通过，`git diff --check` 通过。

## 安全边界

- 事件只含稳定状态和脱敏标识，不含 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链。
- 外部 Webhook 仍受现有 Secret、Outbox、重试和功能开关约束；该切片不改变真实外部投递门禁。
