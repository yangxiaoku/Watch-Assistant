# REQ-012 STRM 增量失败通知证据

日期：2026-07-31

## 完成范围

- dirty worker 在扫描、整理或 STRM 增量重试耗尽时，为无 workflow 的媒体库生成
  `strm.dirty_failed` 错误级站内通知。
- STRM 增量结果明确包含失败项时，即使本地 outbox 已完成消费，也会生成一次可去重的错误通知。
- 关联 workflow 的失败仍由 `workflow.stage_changed` 承载，不额外生成重复的 STRM 通知。
- 通知跳转到现有设置入口，错误详情只包含稳定错误码，不包含远端路径、Cookie、Token、pickcode、
  磁力链接、完整播放 token 或真实直链。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_directory_dirty_worker.py \
  tests/integration/test_notifications_api.py \
  tests/contracts/test_event_catalog.py
```

结果：`11 passed`。

```text
./.venv/bin/python -m ruff check \
  src/watch_assistant/services/directory_dirty_worker.py \
  src/watch_assistant/services/event_catalog.py \
  src/watch_assistant/services/notifications.py \
  tests/unit/test_directory_dirty_worker.py \
  tests/integration/test_notifications_api.py
git diff --check
```

结果：Ruff 和格式检查通过。

## 未覆盖

Webhook 真实接收端、Bark/Telegram、PWA Push 和完整通知事件矩阵仍按 REQ-012/REQ-019 阻断记录处理。
