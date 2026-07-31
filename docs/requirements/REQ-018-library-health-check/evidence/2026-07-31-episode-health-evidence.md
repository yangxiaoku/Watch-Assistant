# REQ-018 剧集完整度健康检查证据

日期：2026-07-31

## 完成范围

- 健康报告接受 REQ-011 的 `EpisodeCompletenessMatrix` 作为只读证据。
- `missing`、`multiple` 和 `unknown` 分别生成稳定的 `CHK-003` 问题及中文影响/建议；无法映射的文件保留为提示。
- 缺集和未知集数不能生成自动修复计划，重复版本只允许人工确认，不触发移动、删除或隔离。

## 验证

```text
./.venv/bin/python -m pytest -q \
  tests/unit/test_library_health.py \
  tests/unit/test_episode_completeness.py \
  tests/integration/test_seasons_api.py \
  tests/integration/test_notifications_api.py
```

结果：24 passed。

```text
./.venv/bin/ruff check \
  src/watch_assistant/services/library_health.py \
  tests/unit/test_library_health.py
git diff --check
```

结果：Ruff 和格式检查通过。

本次验证仅使用脱敏单测/集成夹具，没有调用真实 115 写接口、播放接口或第三方推送。

## 未覆盖

REQ-010 生产库存、自动补集、订阅联动、REQ-012 严重问题通知、直链抽样和复检 worker 仍按阻断记录保持待实现或待验收。
