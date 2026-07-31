# REQ-012 整理操作失败通知证据

日期：2026-07-31

## 完成范围

- 无 workflow 关联的整理 operation 进入 `failed` 状态时，使用稳定事件码
  `organize.operation.failed` 生成错误级站内通知。
- 通知使用整理工作台作为现有可行动跳转，不创建绕过业务状态机的新入口。
- 已关联 workflow 的整理失败仍使用 `workflow.stage_changed` 通知，不额外生成专用失败通知。
- 错误详情只传递整理服务已有白名单错误码，不包含远端路径、Cookie、Token、pickcode、磁力链接、
  完整播放 token 或真实直链。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/contracts/test_event_catalog.py \
  tests/unit/test_notifications.py \
  tests/unit/test_organization_operations.py \
  tests/integration/test_notifications_api.py
```

结果：`22 passed`。

```text
git diff --check
```

结果：通过。

## 未覆盖

Webhook 真实接收端、Bark/Telegram、PWA Push 和完整通知事件矩阵仍按 REQ-012/REQ-019 阻断记录处理。
