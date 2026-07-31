# REQ-019 Webhook 投递运营统计证据

日期：2026-07-31

## 范围

本证据只覆盖离线服务契约，不代表真实 HTTPS 接收端验收完成。

- `WebhookEndpointResponse` 提供端点投递总数、成功数、待重试数、死信数、终态失败率和下一次重试时间。
- 统计从 `webhook_deliveries` 持久记录按端点派生，不新增表、不改变 Outbox 状态机，也不暴露 Secret 或第三方响应正文。
- `failure_rate` 定义为 `dead / (delivered + dead)`；没有终态投递时返回 `null`，待重试记录不计入失败率。
- SQLite 返回的无时区聚合时间在服务层规范化为 UTC，避免客户端误读下一次重试时间。

## 验证

命令：

```text
./.venv/bin/python -m pytest tests/unit/test_webhooks.py tests/unit/test_webhooks_mcp_pwa.py -q
```

结果：`11 passed`。

专项回归构造 1 条成功、1 条待重试和 1 条死信记录，接口派生结果为总数 3、成功 1、待重试 1、死信 1、失败率 0.5，并返回待重试记录的最早时间。

## 未覆盖

- 真实 HTTPS 接收端签名验证、篡改、重放和重启恢复。
- Bark、Telegram、PWA Push 和跨渠道统一健康/死信运营。
- 完整业务事件矩阵的真实触发覆盖。
