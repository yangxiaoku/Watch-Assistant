# REQ-012 关键依赖失败通知证据

日期：2026-07-31

## 完成范围

- 内容检测 worker 无法使用 qBittorrent 依赖时，保留批次失败状态，并发出稳定事件码
  `inspection.dependency_failed`。
- 全局依赖通知使用固定主体 `dependency:qbittorrent`，因此多个受影响批次在 30 分钟去重窗口内
  聚合为一条通知并累加数量。
- 同一依赖故障产生的 `inspection.batch_failed` 不再逐批创建重复站内通知；关联 workflow 仍由阶段通知
  提供任务上下文。
- 通知跳转资源检测设置入口，消息仅包含稳定错误码，不包含 Cookie、Token、pickcode、磁力链接、
  完整播放 token 或真实直链。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/integration/test_notifications_api.py \
  tests/integration/test_inspection_api.py \
  tests/contracts/test_event_catalog.py \
  tests/unit/test_notifications.py
```

结果：`34 passed in 16.53s`。

```text
./.venv/bin/ruff check \
  src/watch_assistant/services/event_catalog.py \
  src/watch_assistant/services/notifications.py \
  src/watch_assistant/services/inspection.py \
  tests/integration/test_notifications_api.py
git diff --check
```

结果：Ruff 和格式检查通过。

## 未覆盖

完整通知事件矩阵、Webhook 真实接收端、Bark/Telegram、PWA Push 和渠道统一运营闭环仍按 REQ-012/REQ-019
的状态保持待实现或待验收。
