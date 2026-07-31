# REQ-009 STRM Operation Ledger Evidence

日期：2026-07-31

## 范围

本证据对应开发分支 `codex/strm-operation-ledger`，只覆盖 STRM 全量、增量和清理请求的
本地 operation 账本，不代表已合入 `codex/publish-main`，也不代表真实 115 播放或写入已验收。

## 交付

- 新增 `strm_operations` 幂等迁移，记录请求类型、媒体库/扫描运行关联、可选 workflow、
  queued/running/succeeded/failed 状态、统计、错误和时间。
- 全量、增量、清理 API 在调用 manifest service 前创建并启动 operation，响应返回
  `operation_id`。
- `GET /api/v1/strm-operations/{operation_id}` 返回终态记录；不存在的记录返回
  `strm_operation_not_found`。
- workflow 的 `strm` 阶段使用真实 operation ID；失败终态幂等，成功终态不能被迟到失败覆盖。
- 前端类型、API client 和中文错误目录已同步；没有写入 Cookie、Token、pickcode、完整播放
  token 或真实直链。

## 离线证据

```text
tests/unit/test_strm_operations.py: 4 passed
tests/unit/test_strm_manifest.py + tests/unit/test_migrations.py: 23 passed
tests/integration/test_strm_operations_api.py + STRM settings regression: 2 passed
ruff check src tests scripts: passed
git diff --check: passed
```

测试使用 SQLite 和本地临时 STRM 输出目录，不调用真实 115、媒体服务器或外部通知接收端。

## 未完成与门禁

- `codex/publish-main` 尚未包含本切片；合并前仍需在最新发布基线重新验证。
- REQ-002/REQ-009 的生产媒体库首次扫描、稳定播放入口、媒体服务器兼容和真实写操作仍由
  `docs/requirements/BLOCKERS.md` 阻断。
- operation ledger 只记录本地请求和 manifest 结果，不解除现有 STRM feature flag、完整扫描、
  Scope、人工确认或清理安全门禁。
