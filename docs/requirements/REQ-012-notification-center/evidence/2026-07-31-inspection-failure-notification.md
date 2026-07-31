# REQ-012 内容检测失败通知证据

## 实现范围

- 独立内容检测批次的 `inspection.batch_failed` 事件生成错误级站内通知。
- 通知跳转资源检测设置，不把检测批次标识误当作推送任务或 workflow。
- 已有 workflow 关联时，带 correlation ID 的同一失败由 `workflow.stage_changed` 提供通知，
  `inspection.batch_failed` 不重复创建站内通知。

## 验收命令

```text
./.venv/bin/python -m pytest -q \
  tests/integration/test_notifications_api.py \
  tests/integration/test_inspection_api.py \
  tests/contracts/test_event_catalog.py

./.venv/bin/python -m ruff check \
  src/watch_assistant/services/notifications.py \
  src/watch_assistant/services/inspection.py \
  tests/integration/test_notifications_api.py

git diff --check
```

结果：专项测试 `28 passed`，Ruff 通过，`git diff --check` 通过。

## 安全边界

- 通知只包含稳定错误码和本地设置跳转，不包含 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链。
- 外部渠道仍复用现有 Secret、Outbox、重试和功能开关，未因该切片自动开启。
