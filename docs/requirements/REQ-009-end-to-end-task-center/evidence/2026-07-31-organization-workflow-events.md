# REQ-009 整理阶段事件与审计关联证据

## 实现范围

- 整理 operation 的排队、claim、完成、失败、不确定、取消和重试状态在提交后发出
  `workflow.stage_changed`。
- `organize.operation.*` 审计事件携带 operation ID、workflow ID 对应的 correlation ID 和受控资源类型。
- 运行中取消仍只记录 `cancel_requested`；远端写入边界和 `uncertain` 语义未改变。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_organization_operations.py \
  tests/integration/test_organization_operations_api.py \
  tests/unit/test_directory_dirty_worker.py \
  tests/integration/test_worker_recovery.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/services/organization_operations.py \
  tests/unit/test_organization_operations.py \
  tests/integration/test_organization_operations_api.py

git diff --check
```

结果：专项测试 `30 passed`，Ruff 通过，`git diff --check` 通过。

## 安全边界

- 审计和阶段事件不记录 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链。
- 整理写入仍受计划确认、幂等、审计、receipt-before-verify 和 `uncertain` 只读核对门禁约束。
