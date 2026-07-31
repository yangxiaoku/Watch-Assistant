# REQ-009 子任务时间线验收证据

日期：2026-07-31

## 范围

本次只验证离线应用和测试数据库中的关联子任务读取，不执行真实 115、STRM 播放、外部通知或其他生产写操作。

## 实现

- `GET /api/v1/workflows/{workflow_id}/children` 支持 `page` 和 `page_size`。
- 聚合 `Task`、`InspectionBatch`、`OrganizationOperation`，并保留只有 workflow 阶段关联的 STRM 子任务。
- 每个返回项同时包含 `status` 与 `stage_status`，避免把模块原始状态误报成顶层 workflow 阶段状态。
- `watchctl workflow children` 复用同一 API。
- Web 任务中心详情加载子任务并支持继续加载下一页。

## 验证结果

- 后端 workflow API 与 CLI 定向测试：`26 passed`。
- 前端 Vitest：`120 passed`。
- Ruff：通过。
- `git diff --check`：通过。
- API 响应脱敏断言：未出现磁力前缀或 `encrypted_url` 字段。

## 未验证项

- 真实 115 写入和远端状态确认。
- STRM 稳定播放入口、HEAD/GET/Range 与媒体服务器兼容性。
- 外部通知接收端和完整跨系统生产验收。
