# REQ-010 库存身份与重复检查契约

## 范围

本契约覆盖完整 `LibraryScanRun` 快照之上的只读派生层。扫描器仍以
115 的 `file_id`/`directory_id` 作为远端稳定身份；派生层不会访问路径、
pickcode 或远端写接口。

## 身份证据

- `object_id`：同一 115 文件的稳定身份，可直接识别精确重复。
- `content_digest` 或 `infohash`：只有调用方提供可信摘要时，才允许生成
  `exact_duplicate`。
- `tmdb_id + media_type`：允许生成媒体身份，并进一步比较解析出的分辨率、
  来源、编码和 HDR 版本字段。
- 文件名解析：只生成 `candidate` 身份。相同候选只能产生
  `review_candidate`/`needs_review`，不能自动阻止推送。
- 人工确认：`PUT /api/v1/libraries/{library_id}/inventory/identities/{object_id}`
  需要 `review:write`（Web 会话或 Agent Scope），只允许对完整快照中的文件
  保存 TMDB/媒体类型/季集字段，并使用 `revision` 防止覆盖其他确认。

## 新鲜度门禁

`GET /api/v1/libraries/{library_id}/inventory` 和
`GET /api/v1/libraries/{library_id}/inventory/check` 均返回：

- `fresh`：完整快照且捕获时间不超过 15 分钟；
- `stale`：完整快照但超过 15 分钟；
- `incomplete`：扫描未完成，检查结果固定为 `index_incomplete`；
- `unknown`：完整快照但没有可用捕获时间。

缺少完整快照时，系统不得返回“库内不存在”的确定结论。

## 脱敏

响应只返回文件 ID、摘要后的身份和统计，不返回 path、pickcode、Cookie、
token、磁力链接或完整播放地址。

## 尚未覆盖

目录级增量事件、跨扫描删除/恢复账本、推送任务自动接入和生产重复处理仍由
REQ-010 后续阶段完成。
