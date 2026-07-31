# REQ-003 `library scan` CLI evidence

日期：2026-07-31

本切片将 `watchctl library scan <library-id>` 接入版本化媒体库扫描 API。

## Contract

- 请求：`POST /api/v1/libraries/{library_id}/scan`
- 请求体只包含 `idempotency_key`。
- CLI 提供 `--idempotency-key` 时原样传递。
- CLI 未提供时生成一次性幂等键，并在结构化结果中回显。
- CLI 不直接访问数据库、115 Cookie 或第三方 API。
- 服务端继续负责 Agent `library:read` Scope、媒体库范围验证、根目录约束和扫描完整度。

## Verification

- `tests/unit/test_watchctl.py::test_library_scan_uses_formal_endpoint_and_returns_idempotency_key`
- 显式幂等键请求体与结果回显均通过。
- 受保护的服务端扫描门禁未被 CLI 绕过。

## Remaining

- `--path-id` 和 `--dry-run` 尚无冻结的服务端 API 契约，因此本切片不开放。
- 生产媒体库完整扫描、稳定播放入口和跨平台 CLI 验收仍未完成。
