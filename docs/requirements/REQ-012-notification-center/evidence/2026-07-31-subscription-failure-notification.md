# REQ-012 订阅检查失败通知证据

日期：2026-07-31

## 完成范围

- 订阅搜索依赖失败后，在持久化 `search_unavailable` 和下一次退避检查时间后发出
  `subscription.check_failed` 事件。
- 通知为错误级，跳转现有设置入口；不包含查询词、资源链接、Cookie、Token、pickcode、磁力链接、
  完整播放 token 或真实直链。
- 正常 `no_match` 结果不产生错误通知。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_subscriptions.py \
  tests/integration/test_notifications_api.py \
  tests/contracts/test_event_catalog.py
```

结果：`12 passed`。

```text
./.venv/bin/python -m ruff check \
  src/watch_assistant/services/subscriptions.py \
  src/watch_assistant/services/notifications.py \
  src/watch_assistant/services/event_catalog.py \
  tests/unit/test_subscriptions.py \
  tests/integration/test_notifications_api.py
git diff --check
```

结果：Ruff 和格式检查通过。

## 未覆盖

订阅确认/自动推送、日历、剧集完结和真实上游验收仍按 REQ-007 阻断记录处理；Webhook 真实接收端、
Bark/Telegram、PWA Push 和完整通知事件矩阵仍按 REQ-012/REQ-019 阻断记录处理。
