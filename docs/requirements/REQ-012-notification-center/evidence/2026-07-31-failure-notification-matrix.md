# REQ-012 失败通知矩阵验收证据

日期：2026-07-31

## 本次范围

在不接入外部通知渠道、不执行生产写操作的前提下，补齐以下关键失败/待处理事件的站内通知：

- `inspection.batch_failed`
- `organize.automation.blocked`
- `organize.operation.failed`
- `strm.verify.completed`（仅 `issues` 状态）

成功的 STRM 校验仍只写日志，不创建站内通知。

## 验证结果

- 通知 API、事件目录契约和通知单元测试：`8 passed`。
- 失败事件均生成中文 ERROR 通知。
- `strm.verify.completed` 的 `verified` 状态被过滤，不产生通知。
- 事件策略只引用已登记事件码；事件目录与必需通知集合契约通过。
- Ruff 与 `git diff --check`：通过。

## 未完成

- 完整需求事件矩阵的逐事件验收。
- 外部通知真实接收端、渠道健康、重试和死信。
- Bark、Telegram 和 PWA Push。
