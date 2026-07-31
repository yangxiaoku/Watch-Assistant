# REQ-012 115 凭据失效通知证据

日期：2026-07-31

## 完成范围

- 115 推送任务得到 `needs_auth` 结果时，任务状态仍保存为 `needs_auth`，并额外发出稳定事件码
  `p115.credentials_expired`。
- 只读 P115 readiness 校验返回 `needs_auth` 时复用同一事件；多个任务或校验结果使用固定主体
  `dependency:p115` 在 30 分钟窗口内聚合。
- `task.failed` 的 `needs_auth` 事件继续保留在结构化日志中，但不再为每个任务创建重复通知。
- 通知跳转 115 设置，消息不包含 Cookie、Token、pickcode、磁力链接、完整播放 token 或真实直链。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/integration/test_notifications_api.py \
  tests/integration/test_worker_recovery.py \
  tests/integration/test_p115_settings.py \
  tests/contracts/test_event_catalog.py
```

结果：`41 passed in 9.79s`。

```text
./.venv/bin/ruff check \
  src/watch_assistant/services/notifications.py \
  src/watch_assistant/services/p115_settings.py \
  src/watch_assistant/worker.py \
  src/watch_assistant/app.py \
  tests/integration/test_notifications_api.py \
  tests/integration/test_worker_recovery.py \
  tests/integration/test_p115_settings.py
git diff --check
```

结果：Ruff 和格式检查通过。

## 未覆盖

真实外部接收端、Bark/Telegram、PWA Push、渠道统一运营闭环和完整通知事件矩阵仍按 REQ-012/REQ-019
的状态保持待实现或待验收。
