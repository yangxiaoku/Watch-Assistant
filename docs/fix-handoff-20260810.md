# 修复交接单（2026-08-10）

由审查会话产出，请工作会话按此清单修复。每个条目含：位置、问题、建议修复方向、严重度。
修复完成后在 PR 描述里逐条标注"已修复/跳过(原因)"，审查会话负责复核。

---

## 🔴 高危（安全，优先）

### H1. agent token 越权写：/api/v1/libraries 下写路由全部 fall-through 到 library:read
- 位置：`src/watch_assistant/security.py:611-614`（`_required_scope` 对 /api/v1/libraries 所有方法返回 `library:read`）
- 受影响路由：`api/library.py:260`（PUT configuration）、`:414`（POST scan）、`:491`（POST scans/{id}/cancel）、`:676`（POST empty-directory-cleanup-plan）、`:933`（POST organization-preview）、`:1281`（PUT inventory/identities/{object_id}）
- 失败场景：只发 `library:read` scope 的 agent token 可触发全库扫描、改配置、创建计划、绑定库存身份
- 修复方向：`_required_scope` 按 HTTP 方法区分——写方法（POST/PUT/DELETE）映射到写 scope（参考 strm.py:597-606 的 strm:write 模式）；为 libraries 引入 `library:write`（或复用 `organize:plan`/`library:write` 语义），并给受影响路由补 `require_scope`
- 参考：`api/strm.py:597-606` 已正确区分读写；objects/delete 已映射到写 scope

### H2. prowlarr.py downloadUrl 无 host/port 校验（受限 SSRF + API Key 泄漏）
- 位置：`src/watch_assistant/adapters/prowlarr.py:409-456`（`_resolve_download_url` / `_follow_redirect_magnet`）、`:216/:224`（全局 X-Api-Key 头）、`:174-177`（pinned transport 端口来自 URL）
- 失败场景：索引器返回恶意 downloadUrl（如 `http://127.0.0.1:9696/...`、`http://127.0.0.1:6379/...`）→ 客户端对 Prowlarr 主机任意端口发带 API Key 的请求；重定向 Location 同样无校验
- 修复方向：
  1. `_resolve_download_url` 解析 URL 后校验：host 必须等于 Prowlarr 端点 host（`_allowed_private_addresses` 已允许的内网 IP 集），port 必须等于端点 port（或 80/443 白名单）
  2. 重定向：`_follow_redirect_magnet` 对 Location 做同样校验，拒绝跨 host/port 跳转
  3. 对校验失败的 URL 直接跳过该条结果（不抛、不计入熔断），保留 `unsupported_count`
- 注意：外部 IP 已被 pinning 挡住，本修复覆盖"Prowlarr 主机内部端口探测 + Host 头注入"边界

---

## 🟠 中危

### M1. save_share 无 remote_ref → 任务永久卡 SUBMITTED
- 位置：`src/watch_assistant/adapters/p115.py:221,694-704`（allow_missing_ref=True → ACCEPTED + remote_ref=None）；`services/tasks.py:884-892`（reconcile 要求 remote_ref）、`:1028-1055`（cancel 只允许 QUEUED 或 UNCERTAIN 无 ref）
- 失败场景：分享已被 115 接收但响应无 task id → 任务永久 SUBMITTED，无法核对/取消/重试
- 修复方向（任选其一，倾向 2）：
  1. `save_share` 成功但无 ref 时返回 UNCERTAIN 而非 ACCEPTED（进入可核对路径）
  2. `cancel` 扩展允许"SUBMITTED 且 remote_ref IS NULL 且无活动租约"（类似上次给 UNCERTAIN 无 ref 的豁免）
  3. reconcile 对无 remote_ref 的任务改为返回"无远端证据"提示而不是硬拒绝
- 附：`services/tasks.py:493-524` SUBMITTED 任务挂新工作流时阶段不同步（M4 同源，一起处理）

### M2. 订阅并发创建竞态：SQLite 唯一约束 NULL 不冲突
- 位置：`models.py:481-490`（uq_subscription_scope 含可空列）、`services/subscriptions.py:59-85`（先查后插，IntegrityError 兜底对电影场景不触发）
- 失败场景：并发创建同一电影订阅 → 重复行 → 重复检测/通知
- 修复方向：应用层加互斥（订阅创建走 `INSERT ... ON CONFLICT` 或加 `asyncio.Lock` 串行化），或迁移加"部分唯一索引"（SQLite 支持 `CREATE UNIQUE INDEX ... WHERE season_number IS NULL` 两态索引）
- 补并发测试：`tests/integration/test_subscriptions_api.py` 仅 1 个测试

### M3. backups 路由 scope 缺口
- 位置：`security.py:621`（默认回退）+ `api/backups.py` 无 require_scope
- 失败场景：默认 agent token（system:read）可读备份清单/恢复预览；task:write 可创建全库备份
- 修复方向：`_required_scope` 为 `/api/v1/backups` 加映射（GET→`system:read` 或新 `backup:read`；POST/PUT→`backup:write`），并在 `api/backups.py` 加 `require_scope`
- 补映射测试（test_security.py 目前只测 strm 与 organization-plans）

### M4. strm cancel 绕过租约围栏（待验证终态行为）
- 位置：`api/strm.py:771`（cancel 不传 lease_owner）、`services/strm_operations.py:417-427`（lease_owner None 时无条件取消）
- 失败场景：运行中的操作被 API cancel 无条件置 CANCELLED + 清租约 → 执行器后续终态写入 rowcount=0 被吞
- 修复方向：`api/strm.py` 的 cancel 路由从当前请求上下文获取 lease owner 传入；或 `operations.cancel` 在操作 RUNNING 且无 lease_owner 时拒绝（409），要求显式强制参数
- 验证：先确认 `fail()/complete()` 在 rowcount=0 时的行为再定

### M5. 前端：ResourceTable 分页失效时重试按钮不可达（死分支）
- 位置：`frontend/src/components/ResourceTable.vue:170-171` + `App.vue:1653`
- 失败场景：`paginationUnavailable=true` 时 `v-if` 分支挡住 `v-else-if="resourceError"` 的重试按钮 → App.vue:1653 的 retry-page 处理器是死代码
- 修复方向：`paginationUnavailable` 分支也渲染重试按钮（同一 onClick 走 `@retry-page`）

### M6. 前端：resourceItems 旧快照掩盖新搜索结果
- 位置：`frontend/src/App.vue:199-207`（computed 优先 resourceResponse）、`:1073-1074`（preserve）、`:1095`（legacy 回退）
- 失败场景：刷新后 legacy 回退把新结果写入 fallback，但 computed 仍读旧 resourceResponse → 新结果被掩盖，提示"当前显示搜索快照结果"有误导
- 修复方向：legacy 回退成功时也更新 `resourceResponse`（或清空旧快照再展示 fallback），与搜索失败路径（:1049-1056 清空）语义一致

### M7. 前端：OrganizationResultPanel 卸载竞态
- 位置：`frontend/src/components/OrganizationResultPanel.vue:65-78`（loadOrganizationResult 无 mounted 门禁）、`:87-109`、`:120`
- 失败场景：初始请求在途时切走 tab → 卸载后请求返回 → pollOrganizationResult 仍启动轮询 2 分钟
- 修复方向：loadOrganizationResult 解析后检查 mounted（参照 SettingsView settingsMounted 模式）再启动轮询

### M8. 前端：userscript 跨影片导航竞态
- 位置：`frontend/src/userscript.ts:144-169`
- 失败场景：影片 A 搜索在途时导航到 B → A 的响应渲染进 B 的面板
- 修复方向：`load()` 在 await 之后、renderPanel 之前重新校验 `extractMovieId(window.location.pathname) === movieId`，不一致则丢弃

### M9. 前端：资源搜索框输入被旧请求错误重置
- 位置：`frontend/src/components/ResourceTable.vue:57-58`（watch resourceError 时重置 queryDraft）
- 失败场景：新输入请求失败 → 正在输入的文字被抹掉
- 修复方向：仅当失败时回退到"已提交的查询"且该查询与 draft 不同才重置；或去掉该 watch

---

## 🟡 低危（可批量）

- **L1** `services/search.py:443`：异步搜索任务把 TmdbNotFoundError 归一化为 tmdb_unavailable → 应 media_not_found
- **L2** `api/tasks.py:76,134,201`：PushKindUnsupported 映射 503 → 语义更适合 409/403
- **L3** `services/api_errors.py:317-375`：17 个错误码未注册 _CATALOG（走通用文案）；`invalid_lease_duration: 500` 应为 4xx
- **L4** `security.py:454-482`：限流按 client.host 内存统计，未用 X-Forwarded-For（反代下全局互锁）；登录限流无测试（补 test_auth_api.py）
- **L5** prowlarr.py 默认分页 page_size=1 × 20 页 = 最多 20 条（主调用方显式传 limit 不受影响，但 CLI/其他调用方会踩）
- **L6** `security.py:485-493`：security_manager 缺失时 fail-open（测试/live runner 路径）→ 应 fail-closed
- **L7** `adapters/p115.py:41-49,719-735`：_AUTH_MARKERS 子串误判认证失败（"授权"字样出现在业务错误里）
- **L8** `adapters/qbittorrent.py:359-372`：清理失败覆盖已 VERIFIED 结果
- **L9** `services/organization_worker.py:217-232`：异常无退避热循环（对比 worker.py:234-251 有退避）
- **L10** 前端：App.vue loading 死状态（:49/:703/:1604）、SettingsView 目录 ID toLowerCase（:716-718）、resourceCacheKey 读写键不一致（:893/:932）、LogsView/NotificationCenterView/OrganizationHistoryView 缺卸载门禁
- **L11** 后端：p115 get_status 只翻 100 页（:251）

---

## 修复纪律

1. 每个修复独立提交，PR 描述逐条对应上述编号
2. 修复后必须：受影响单测 + ruff + 前端 vitest 全绿
3. 高危（H1/H2）加回归测试（security scope 映射测试、prowlarr URL 校验测试）
4. 中文注释/错误文案
5. 工作树冲突：发布分支工作树请勿与其他会话并发编辑；建议在独立分支 worktree 修复
