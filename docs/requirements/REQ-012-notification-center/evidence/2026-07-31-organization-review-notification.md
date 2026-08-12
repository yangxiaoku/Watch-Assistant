# REQ-012 整理待确认通知证据

日期：2026-07-31

## 完成范围

- 整理预览状态为 `needs_review` 时，API 预览和自动预览均记录 `organize.needs_review`。
- 事件摘要使用中文、文件数量和整理预览对象 ID，不包含路径、Cookie、Token 或第三方原始响应。
- 同一计划在 30 分钟去重窗口内重复生成预览时复用通知并累加 `aggregate_count`，不会重复轰炸用户。
- 通知 `action_type=organization_plan` 会导航到整理工作台，满足可行动通知的定位要求。

## 验证

```text
./.venv/bin/python -m pytest tests/integration/test_organization_preview_api.py tests/unit/test_organization_automation.py tests/contracts/test_event_catalog.py -q
```

结果：`5 passed`。

```text
cd frontend && env PATH=/Users/apple/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH ./node_modules/.bin/vitest run tests/NotificationCenterView.spec.ts
```

结果：`5 passed`。

Vite production build 成功。

## 未覆盖

完整通知事件矩阵、Bark/Telegram、PWA Push 和真实外部接收端仍按 REQ-012/019 的阻断记录处理。
