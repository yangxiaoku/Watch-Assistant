# REQ-003 整理计划 CLI 证据

## 实现范围

- `watchctl organize plan --library <id> [--path-id <directory-id>]` 先读取媒体库最新扫描摘要。
- 仅当扫描状态为 `completed` 且 `complete=true` 时，CLI 才调用
  `POST /api/v1/libraries/{library_id}/organization-preview`。
- 请求复用服务端 `OrganizationPreviewService`，可按已验证扫描快照中的目录 ID
  限制源文件范围。
- 该命令只创建可审核的本地整理计划，不调用组织操作排队或任何 115 写入接口。

## 安全与失败门禁

- 没有完整扫描时返回稳定错误码 `library_scan_required`。
- 未找到源目录时由 API 返回 `source_directory_not_found`，不会扩大扫描范围。
- CLI 不读取数据库、Cookie、任意远端路径或第三方 API。

## 验收命令

```text
pytest -q tests/unit/test_watchctl.py tests/unit/test_organization_preview.py tests/integration/test_organization_preview_api.py
ruff check src/watch_assistant/cli.py src/watch_assistant/schemas.py src/watch_assistant/api/library.py src/watch_assistant/services/organization_preview.py tests/unit/test_watchctl.py tests/unit/test_organization_preview.py tests/integration/test_organization_preview_api.py
git diff --check
```

本证据不包含 Cookie、Token、磁力链接、pickcode、完整播放 token 或真实直链。
