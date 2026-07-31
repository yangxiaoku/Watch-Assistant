# REQ-009 workflow 时间筛选证据

日期：2026-07-31

## 完成范围

- workflow 列表在分页前按 `updated_after` 和 `updated_before` 过滤。
- 时间范围使用服务端 `updated_at`，边界包含；反向范围返回 `422 invalid_request`。
- `watchctl workflow list` 透传两个时间参数，仍使用版本化 API，不读取本地数据库。

## 验证

```text
.venv/bin/python -m pytest -q \
  tests/integration/test_workflows_api.py \
  tests/unit/test_watchctl.py
```

结果：25 passed。

```text
.venv/bin/ruff check \
  src/watch_assistant/api/workflows.py \
  src/watch_assistant/services/workflows.py \
  src/watch_assistant/cli.py \
  tests/integration/test_workflows_api.py \
  tests/unit/test_watchctl.py
git diff --check
```

结果：Ruff 和格式检查通过。

本次验证仅使用离线数据库和 HTTP 夹具，没有调用真实 115 写接口、播放接口或第三方推送。

## 未覆盖

REQ-009 的完整业务事件矩阵、Agent 维度筛选、外部通知渠道、生产 115/STRM 和真实部署验收仍按阻断记录处理。
