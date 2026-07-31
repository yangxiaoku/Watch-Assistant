# REQ-003 STRM 只读校验证据

## 实现范围

- `watchctl strm verify --library <id>` 先读取媒体库的最新扫描摘要。
- 只有扫描状态为 `completed` 且 `complete=true` 时，CLI 才调用正式的
  `POST /api/v1/libraries/{library_id}/strm-verify` API。
- 服务端要求 `strm_full_enabled` 和 `strm:read`，并再次验证媒体库范围、扫描快照
  完整性及当前版本。
- 校验只读取当前受管 manifest 和本地 STRM，报告 `missing_manifest`、
  `orphan_manifest`、`path_mismatch` 和 `invalid_content`；不会修复文件、退休 manifest
  或调用 115 写入。

## 安全边界

- STRM 内容只能匹配配置的稳定 Watch Assistant 播放入口和不透明 manifest ID。
- 校验拒绝路径穿越、符号链接逃逸、不可读输出目录和不安全播放入口配置。
- 输出只包含库 ID、扫描版本、不透明 manifest ID 和稳定错误码，不包含 Cookie、Token、
  pickcode、完整播放 token、磁力链接或真实直链。

## 验收命令

```text
./.venv/bin/python -m ruff check \
  src/watch_assistant/services/strm_verification.py \
  src/watch_assistant/api/strm.py \
  src/watch_assistant/schemas.py \
  src/watch_assistant/cli.py \
  src/watch_assistant/services/event_catalog.py \
  tests/unit/test_strm_manifest.py tests/unit/test_watchctl.py

./.venv/bin/python -m pytest -q \
  tests/unit/test_strm_manifest.py \
  tests/unit/test_watchctl.py \
  tests/contracts/test_api_error_catalog.py \
  tests/contracts/test_event_catalog.py \
  tests/integration/test_settings_api.py

git diff --check
```

结果：Ruff 通过，专项测试 `45 passed`，`git diff --check` 通过。

## 未覆盖范围

本证据只证明离线只读校验契约。生产媒体库完整扫描、稳定播放入口的真实可用性、
元数据同步、Emby/Jellyfin/Plex 兼容性和 Windows/Linux/macOS 端到端验收仍未完成，
因此不能据此宣称 REQ-002 或生产 STRM 已上线。
