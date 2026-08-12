# REQ-009 推送阶段事件证据

## 实现范围

- 推送任务创建和复用关联 workflow 时记录 `push` 阶段 `running`。
- 手动重试、排队取消、worker 接受/失败/不确定和过期租约恢复在子任务事务提交后发出
  `workflow.stage_changed`。
- 事件携带 workflow ID、correlation ID、阶段、状态和稳定错误码；原有 `task.*` 生命周期事件保留。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/integration/test_worker_recovery.py tests/integration/test_tasks_api.py \
  tests/integration/test_workflows_api.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/services/tasks.py src/watch_assistant/worker.py \
  tests/integration/test_worker_recovery.py

git diff --check
```

结果：专项测试 `20 passed`，Ruff 通过，`git diff --check` 通过。

## 安全边界

- 阶段事件不包含 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链。
- 事件只记录本地任务和 workflow 标识；远端 uncertain 仍禁止自动重试或伪造撤回。
