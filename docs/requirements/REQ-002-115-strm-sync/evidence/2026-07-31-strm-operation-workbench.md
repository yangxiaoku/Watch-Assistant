# REQ-002 STRM 操作工作台证据

日期：2026-07-31

## 覆盖范围

- `LibraryWorkbenchView` 在 STRM 请求返回 operation ID 后查询
  `GET /api/v1/strm-operations/{operation_id}`，并在重新加载时调用按媒体库分页的
  `GET /api/v1/libraries/{library_id}/strm-operations`。
- 工作台展示全量、增量或清理类型、中文终态和生成/未变化/跳过/失败/退休统计。
- operation 错误通过前端错误目录转换为中文引导，不直接把错误详情扩展到敏感信息。
- 媒体库切换时清除上一个媒体库的 operation 展示，避免跨范围误读。
- operation 历史使用游标分页，服务端按创建时间和 operation ID 稳定排序。

## 验证

- 前端 Vitest：`116 passed`。
- 前端生产构建：通过。
- 后端 STRM operation 单元/集成：`6 passed`。
- Ruff：通过。
- `git diff --check`：通过。

## 未覆盖

- 真实 115 播放入口、媒体服务器兼容性和生产媒体库首次扫描。
- 服务端异步轮询和失败请求在 HTTP 错误响应中的 operation ID 返回。
- 本证据不启用任何 STRM 或 115 生产写入开关。

本文件不包含 Cookie、Token、pickcode、磁力、完整播放 token 或真实直链。
