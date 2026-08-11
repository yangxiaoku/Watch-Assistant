# Watch-Assistant 全项目代码审查报告

- 日期：2026-08-11
- 审查方式：superpowers 并行只读审查（6 个独立领域子代理 + 独立验证），覆盖后端核心/API、整理库存服务、STRM/任务/搜索服务、适配器层、前端、测试/部署/配置
- 基线：`codex/publish-main` @ `5143360`
- 审查原则：只读，不修改任何文件；只报告实际读过且可给出行号的发现；推测项已标注

## 验证基线（本次实测）

| 检查 | 结果 |
|------|------|
| ruff lint | ✅ 通过 |
| 后端单元测试 `tests/unit` | ✅ 1145 passed |
| 后端集成测试 `tests/integration` | ❌ **2 failed / 429 passed** |
| 前端 vitest | ✅ 231 passed |
| 硬编码凭据扫描 | ✅ 未发现（仅动态生成的 webhook secret） |

**集成测试回归（本报告独立确证）：** `tests/integration/test_library_scan_scope_recovery.py` 有 2/3 测试**确定性失败**，根因已二分定位到提交 `fb87c57`（2026-08-11 "gateway 传输层暴露 app 读接口并只对 405 回退"）：
- 该提交让 P115 gateway 优先调用 `fs_files_app`/`fs_info_app`，且仅对 HTTP 405 回退旧接口、其余异常 fail-closed
- 但测试 mock `_Transport` 只实现旧接口 `fs_files`/`fs_info`，未实现 app 方法 → mock 抛 `AttributeError` → 被 fail-closed 当作真实失败上抛 → 2 个测试挂
- 生产传输类（`P115FixedReadOnlyTransport`/`P115C03LiveTransport`）已实现新方法，所以是**测试套件未随协议变更同步**，非生产 bug；但当前 `scripts/verify.sh` 门禁为红
- 修复：给测试 `_Transport` 补 `fs_files_app`/`fs_info_app`（返回与旧接口相同内容）

---

## Critical（必须修）

### 1. 自动清理路径绕过全部 115 写门禁（整理/库存）
- **位置**：`organization_automation.py:438`（调用点）、`_auto_clean_source:779-840`、`_delete_small_files:922-960`、`_cleanup_empty_directories:999-1062`
- **问题**：`_run` 生成预览后**无条件**调用 `_auto_clean_source`，其中：
  - `_delete_small_files` 把"review 动作且 < `small_file_threshold_mb`（默认 100MB）"的文件直接 `prepare_delete`（回收站）
  - `_cleanup_empty_directories` 无条件最深优先删除空目录
  - 这条路径**不经过** `OrganizationWriteGate`/契约校验、**不经过**计划确认、**不尊重** `manual_confirmation`（人工确认模式下移动/重命名被挂起，但删除照样执行）
  - `_cleanup_empty_directories` **完全忽略 `settings.cleanup_empty_directories`（默认 False）**——运维显式关闭空目录清理，自动化仍会执行
- **为什么重要**：直接违反 AGENTS.md "所有 115 写操作必须 fail-closed（契约+预览+人工确认+幂等+审计）"。其余子系统（worker/executor/outbox）都严守门禁，唯独这条自动清理是裸写。
- **修复**：(a) `_auto_clean_source` 显式接收 `cleanup_empty_directories` 与 `manual_confirmation`，两项都满足才执行对应清理；(b) 空目录清理受配置开关控制；(c) 删除路径复用 `evaluate_organization_write_gate`。

### 2. 永久删除身份碰撞防护可被 size 缺失绕过 → 误永久删除风险（适配器）
- **位置**：`p115_permanent_delete_transport.py:140-151`（配合 `p115_delete.py:141-152`）
- **问题**：匹配条件 `size_bytes is None or item.size_bytes is None or item.size_bytes == size_bytes`——只要**任一侧** size 为 None 就退化为按 `(parent_id, name)` 匹配。`p115_delete.py:145` 传入的 `entry.size_bytes` 来自扫描快照可能为 None；回收站 `_normalize_entry` 在提供方省略 `file_size` 时也为 None。此时若在 ~8 秒轮询窗口内另一进程删除了同目录+同名文件，`find_new_entries` 会把它当唯一稳定候选返回，随后 `permanently_clean` 对其执行**不可逆**的 `recyclebin_clean`。
- **为什么重要**：`recyclebin_clean` 永久删除、无回收站兜底；两帧稳定性只滤噪声、不鉴别身份。这是真实的数据丢失路径。
- **修复**：size 缺失时 fail-closed 拒绝清理（要求严格相等或返回 None）；或改用 `recycle_id` + 删除前 `fs_info` 做强身份绑定。

### 3. STRM 操作状态机启动恢复误杀 QUEUED 操作（STRM/任务）
- **位置**：`strm_operations.py:913-919`（配合 `app.py:1329`）
- **问题**：每次应用启动，`recover_incomplete()` 会把**所有尚未认领的 QUEUED 操作终态化为 FAILED**。它走 `_recover(cutoff=None, respect_lease=True)`，WHERE 是 `lease_expires_at IS NULL OR <=now`；QUEUED 操作创建时 lease 恒为 NULL（`strm_operations.py:112-121`，`claim_start` 才写 lease），因此**全部命中**被置为 `FAILED/strm_operation_recovered`。`recover_stale` 有 `max_age` cutoff（894-912）不受影响，唯独 `recover_incomplete` 没有。
- **为什么重要**：多 worker 部署时一个 worker 启动会误杀另一个 worker 刚 create、即将 claim 的操作；FAILED 不会自动回到队列，违反"终态不可误杀"。
- **修复**：给 `recover_incomplete` 加 cutoff（如 `updated_at<=now-5min`），或仅恢复 RUNNING 且租约过期的操作，QUEUED 一律跳过。

### 4. 任务租约过期回收可吞掉一次已成功的远端提交（任务/worker）
- **位置**：`worker.py:196-221`（配合 `tasks.py:698-745, 880-886`）
- **问题**：外部提交耗时超过租约（60s）或写锁竞争使 `renew` 持续失败 → 租约过期；周期 `recover_expired` 经 `claim_expired` 抢占换掉 lease_owner/token，此时 `lease.remote_ref` 仍为 None → 跳过只读核对，直接置 `UNCERTAIN/REMOTE_OBSERVATION_MISSING`。原 worker 的提交已成功但结果被 `_LeaseClaimLost` 丢弃（worker.py:444-461）。此后 `remote_ref=None`，`retry`/`reconcile` 均拒绝，只有 cancel 可走 → **成功提交的远端文件永久无法认领**。重复提交已被 fencing 防住，但结果丢失没有兜底。
- **修复**：回收抢占时即使 remote_ref 为空也按资源做一次只读远端探测；或给"提交在途"加显式 in-flight 标记让回收器不抢占。

### 5. Webhook 投递无原子 in-flight 认领 → 重复 POST（webhook）
- **位置**：`webhooks.py:261-316`（配合 `app.py:1437-1451`、`api/webhooks.py:107-110`）
- **问题**：`publish_due` 选行后 `_deliver` 仅在 304 行重读并查 `status=="pending"`；HTTP 投递期间（最长 10s）行保持 pending。后台 worker 每 ~5s 调一次，且 `POST /publish-due` 可并发触发，响应慢于轮询周期的端点会被重新选中、两个调用各自 POST 同一 payload（同一 event_id/签名）。
- **修复**：投递前原子认领（`UPDATE ... SET status='in_flight', next_attempt_at=now+lease WHERE id=? AND status='pending'`，rowcount==1 才继续）。

### 6. 搜索刷新/预热路径链接校验无总时长上限（搜索）
- **位置**：`search.py:970-981 + 1502-1523`（配合 `pansou.py:120-175`）
- **问题**：`_verify_shares` 在 `_search_locks` 锁内执行（583-590），`refresh=True` 即触发；`check_links` 每批超时 `max(12,len*12)`=120s、3 并发 gather 无全局 deadline：200 个 share 挂死服务端可拖 ~840s，期间该媒体所有搜索/预热排队。同步 `/search`（api/search.py:227-235）无外层超时，请求挂死。
- **修复**：`check_links` 外层加 `wait_for`（如 30s）超时降级为 `link_check_inconclusive`。

### 7. Dockerfile 引用不存在的 `config/` 目录，Compose 部署无法构建（部署）
- **位置**：`Dockerfile:27` `COPY --chown=... config/ /app/config/`
- **问题**：全仓无 `config/` 目录、无 `.dockerignore`。`docker compose up -d --build`（README:107-113 文档化的备用部署路径）会 `COPY failed`。CI 只跑 `docker compose config --quiet`（仅校验 YAML、不构建），该 bug 完全逃逸所有门禁。
- **修复**：删除该 COPY 行（`config.py` 并不读 `/app/config`，疑为残留），并在 CI 加真实 `docker compose build`。

---

## Important（应当修）

### 后端核心/API
- **`security.py:579-634`** — 集中式路径→scope 映射对**未知路径 fail-open**（默认 GET→`system:read`、写→`task:write`）。已造成 `/api/v1/organization-history` GET 落 `system:read`（过度限制）、`/api/v1/imports` 与 `/api/v1/workflows` 写方法只要求 `task:write`（范围过宽）。任何未来新增写端点漏配清单会静默降级为 `task:write`。修复：未知路径 fail-closed（403）+ 显式补 scope。
- **`services/subscriptions.py:205-223`** — `check()` 绕过 `mutate()` 的 revision 乐观锁，在"重读终态→commit"间是无条件 `revision += 1` 的读-改-写窗口；并发 `pause/resume/cancel` 提交在此窗口会被 `check()` 把 PAUSED/CANCELLED 盖回 MATCHED——复活用户明确暂停/取消的订阅，mode=DOWNLOAD 时触发非预期下载。修复：终态写入改条件 UPDATE（`WHERE ... status NOT IN (PAUSED,CANCELLED,COMPLETED)`），rowcount!=1 抛 `subscription_not_active`。
- **`api/settings_p115.py:258-288`** — QR 扫码轮询 `GET /settings/p115/qrcode/{session_id}` 在轮询成功后直接持久化 115 Cookie。GET 豁免 CSRF 且无限流，是"非 GET 必须 CSRF + 写限流"门禁的唯一例外。修复：改 POST + 限流，拆"只读轮询"与"确认入库"两步。
- **`api/settings.py:635-696`** — `GET /logs/export` 用 `while True` 全量累积进内存，无上限。已认证 DoS。修复：加导出上限 + 流式输出。
- **`services/mcp.py:156-171 + 525-534`** — MCP `tasks.list/get` 对 ORM 对象 `json.dumps` 抛 `TypeError` → 裸 500。持有 `task:read` 的 agent 调用必崩。修复：MCP 层返回公开任务视图，`_model_dump` 对未知类型 fail-closed。
- **`api/organization_plan.py:109-112` + `schemas.py:903`** — `confirm_organization_plan` 仅当 `plan_hash is not None` 才校验摘要（默认 None），与 operation 排队对 bearer agent 强制 digest 不一致。修复：bearer 上下文强制要求 plan_hash。
- **`api/settings.py:447-458 + services/settings.py:1184-1218`** — 目录范围校验是条件性的：`organization_target_root_id` 为空时整段跳过，root 未配置时唯一兜底只剩运行期 write-contract。修复：root 未配置时对 target/source/push 目录 ID fail-closed。

### 整理/库存服务
- **`organization_operations.py:328-401` + `organization_worker.py:267-387`** — `uncertain` 操作在计划被新扫描/refresh 失效（`plan.revision` 递增）后，`reconcile_once` 因 `expected_plan_revision` 失配恒返回 `scope_unverified`，该操作**永久卡死**在 UNCERTAIN，且 `_plan_has_active_operation` 阻止对该计划创建新操作，只有人工 `invalidate_plan` 才解套。修复：reconcile 检测 revision 失配时把该 UNCERTAIN 终态化为 FAILED（`plan_prerequisites_changed`）并 invalidate 计划。
- **`organization_worker.py:221-232`** — executor 抛意外异常时用**陈旧租约**调 `finish_after_lease_loss`：executor 每次 `_renew` 使 revision+1，而 except 分支用的是 `claim_next` 返回的旧 lease，CAS 因 revision 失配失败被吞 → 写操作已发生但操作停留 ORGANIZING 最长 5 分钟，错误码退化为泛化 `outcome_unknown`。修复：executor 返回结果携带最终 lease 状态，worker 兜底用当前租约终态化。
- **`organization_automation.py:922-960`** — `_delete_small_files` 删除前 live 复核只匹配 `id+name`，不复核 size；"小文件"若在扫描后被续传变大（下载中）会被按旧快照回收进回收站。修复：live 复核携带 size 时要求一致，或"最近修改的文件一律跳过"。
- **`organization_operations.py:1296`** — `_record_history` 完成事务内每 action 一次 SELECT（N+1），大批量计划拖长临界完成事务、放大 SQLite 锁竞争。修复：一次性 `IN(...)` 查出已存在条目。
- **`organization_operations.py:338-364`** — 过期 ORGANIZING→UNCERTAIN 是 SELECT 后 ORM 逐行改，无原子 CAS（多实例下 revision 可能 +2、重复发 workflow 事件）。修复：改原子 `UPDATE ... WHERE status==ORGANIZING AND lease_expires_at<=now`。

### STRM/任务/搜索
- **`tasks.py:940-948`** — `reconcile` 能复活终态 FAILED 任务（守卫未排除 FAILED）。修复：reconcile 显式拒绝 FAILED，或 `apply_remote_status` 对 FAILED 加终态守卫。
- **`inspection.py:496-536` + `workflows.py:1349-1453`** — 检测批次完成后 INSPECTION 工作流阶段**永远停在 RUNNING**，后续 APPROVAL/PUSH 永久 `workflow_prerequisite_not_met`。修复：批次终态处调 `sync_child_stage` 置 SUCCEEDED/SKIPPED/FAILED。
- **`strm_manifest.py:545-550 + 387-390`** — `uncertain` 结果在常见路径被折叠成普通 `failed`，操作 ledger 丢失"需只读核对"信号，运维无法区分"可安全重试"与"必须先核对远端"。修复：捕获处单独识别 `uncertain` 收口为 `strm_operation_uncertain`。
- **`strm_cleanup_plan.py:404-421 + 375-377`** — 无 `applying` 中间态，两个绕过 ledger 的并发 `apply_plan` 会竞态产生**孤儿 .strm 文件**（后提交者 CAS 失败回滚把文件写回磁盘，而 manifest 已 retire）。修复：引入 `applying` 占位态，或 CAS 失败分支先只读确认再回滚。
- **`empty_directory_cleanup_plan.py:462-467`** — 部分远程删除已生效后，非 lease 错误仍返回具体错误码而非 `empty_cleanup_uncertain`，调用方不知道已删了几个目录。修复：`remote_side_effect_started` 为真时所有非成功路径返回 uncertain 并写出已回收目录数。
- **`p115_login_devices.py:85`** — `add_device` 响应 `active` 是陈旧 False（Core 级批量 UPDATE 不同步 ORM 对象）。修复：commit 前 `session.refresh(device)`。
- **`mcp.py:377`** — `hmac.compare_digest` 对非 ASCII digest 抛未捕获 `TypeError` → MCP 请求崩溃。修复：digest 限 ASCII hex 或 try/except 映射 `McpError`。
- **`prowlarr_settings.py:548-556`** — `_resolve_hostname` 在事件循环内做阻塞 `socket.getaddrinfo`，解析慢时卡住整个事件循环。修复：`asyncio.to_thread`。
- **`search.py:1049-1081 + api/search.py:227-235`** — 实时搜索整条路径无总预算（90s 预算只在后台资源搜索任务），抽风索引器可让一次搜索远超预期。修复：给 `_query_prowlarr`/`_query_sources` 加 `wait_for` 并降级 warning。
- **`search.py:191-198`** — `_search_locks`/`_resource_search_tasks` 等四个 dict 只增不减，长跑实例内存无界增长。修复：TTL 淘汰或 LRU 上限。

### 适配器
- **`p115.py:368-379 + 1112-1122`** — 默认只读网关未启用 `allow_virtual_root`，`parent_id="0"` 目标下载任务恒返回 `availability_observer_unavailable`（UNCERTAIN），根目录目标的下载永久无法 AVAILABLE。目录选择器（`api/settings_p115.py:217`）已正确传参，唯独 `p115.py` 可用性核对路径漏传。修复：`_readonly_gateway_for` 在 `parent_id == "0"` 时传 `allow_virtual_root=True`。
- **`p115_c03_live_transport.py:358-365 + p115.py:570-586`** — 990009 busy 重试无差别施加于非幂等写操作（fs_mkdir/fs_move/fs_rename/fs_delete、share_receive）：首请求实际落库而响应误报 busy 时，重试会重复建目录（孤儿重复目录）或重复接收分享。修复：只对真正幂等操作启用 busy 重试，非幂等写把 busy 当 UNCERTAIN 交给调用方只读核对。
- **`p115_playback_transport.py:97`** — `raise ... from error` 链起 p115client 原始异常，若某处 `logging.exception` 会带出含完整动态链接/签名参数的 traceback（潜在泄漏点）。修复：`from None`。
- **`p115.py:498-505`** — `_client_for_operation` 工厂 `asyncio.wait_for(to_thread(...))` 超时后线程仍继续运行，构建出的 client（含连接池）泄漏。修复：与 `submit_magnet` 的超时丢弃模式一致，或 factory 内保证超时后自清理。
- **`p115_library_gateway.py:224-233`** — app 接口返回"结构化 405"（Mapping 含 status_code:405，不抛异常）时不会回退旧接口，被误判为 `pagination_unverified` 失败。修复：`_call` 先检查结构化 405 再决定回退。

### 测试/部署
- **`adapters/tmdb.py:101` + `pansou.py:77`** — httpx 未设 `trust_env=False`，环境代理可截获 TMDB API Key（项目自审 H10 高危、交接单只修了 qbittorrent，此处漏修）。修复：与 prowlarr 一致显式 `trust_env=False`。
- **`services/p115_delete.py:73-74/101-110/111-113`** — 永久删除三个核心 fail-closed 分支（未确认/前置条件变化/凭据缺失）在 `tests/unit/test_p115_delete.py` **零测试覆盖**（fixture 恒 `confirmed=True`、cookie 恒有效）。
- **`api/library.py:109-119`** — 永久删除端点 4 道门禁开关只测了 1 道（`tests/integration/test_library_configuration_api.py:416-421` 只断言 `organization_write_unverified`）。
- **`services/inventory_push_guard.py:86-88/104-105`** — 三个 fail-closed 分支（resource 不存在/非 magnet/scope 未配置）无测试（fixture 恒 `kind="magnet"`、`scope_verified=True`）。
- **`services/empty_directory_cleanup.py`** — 契约测试覆盖了 scope/lease/SUCCESS，但 `scope_confirmed` 校验、`cleanup_observation_unverified`、SKIPPED、postcondition 系列、`_safe_name` 拒绝分支均未覆盖；`tests/integration/test_empty_directory_cleanup_api.py:171` 用 mock 掉真实 cleaner。
- **Flaky 时序断言**：`test_organization_worker.py:500-508/540-546`（sleep 0.25/2.1s 后断言调用次数）、`test_worker.py:114`（sleep 0.05s 后断言 renew_calls==0）、`test_worker_recovery.py:335,381`（毫秒级租约 + sleep 0.08s）——CI 负载下事件循环抖动会导致误失败/误通过。修复：用 `asyncio.Event`/条件变量或 `_wait_for(predicate)` 轮询。
- **空断言测试**：`test_qbittorrent_adapter.py:240`（204 用例无断言）、`test_cache_warm.py:131`、`test_business_logging.py:63`、`test_organization_scheduler.py:128`。
- **`AGENTS.md:120-121`** — "systemd SQLite 备份路径待确认"已过时，与 `README.md:216-226` 和已实现的 `scripts/systemd_backup.py` 矛盾。
- **`docker-compose.yml:42`** — `ORGANIZATION_WRITE_CONTRACT_VERIFIED` 是死配置（config.py:200-201 明确忽略，`extra="ignore"` 静默丢弃），设 true 无效且与 `.env.example:47` 说明不一致。修复：移除该行。

### 前端
- **`App.vue:1546-1580` + `api.ts`（无 401 拦截）** — 登录态失效后无恢复路径：`api.me()` 只在启动时调用一次，后端会话过期（重启/CSRF 轮换）后所有请求 401/403，但前端无全局拦截把状态打回登录门，所有写操作静默失效、只能整页刷新。修复：`ApiClient.request` 检测 401/403 发全局事件，App.vue 置 `authenticated=false` 回登录界面。
- **`api.ts:705`** — 非 JSON 成功响应（`200` 但 `Content-Type` 非 JSON）被 `response.json().catch(()=>({}))` 吞成 `{}`，视图 `[...undefined]` 抛异常白屏。修复：`response.ok` 但非 JSON 时抛 `ApiError("invalid_response")`。
- **`App.vue:1488-1511`** — 任务轮询 `setInterval` 不等待上一轮完成，慢网络下请求堆积。修复：链式 `setTimeout` 或 `isPolling` 标志。
- **`SettingsView.vue:911-918`** — 保存成功把单一 revision 广播到所有设置块（推测；若后端各块独立 revision 则是真实保存缺陷）。修复：仅更新刚保存块的 revision。
- **`OrganizationWorkbenchView.vue:292`** — 确认并整理后本地捏造 `plan.revision + 1` 而非从服务端取真实 revision，后续可能 `stale_revision`。修复：操作后重新拉取计划。
- **`App.vue:181/844`** — `seasonDetailCache` 无限增长 Map（对比其他缓存 cap 20）。`taskPolling.ts:10-24` 的 versions Map 也无清理。修复：加容量上限。

---

## Minor（可选，摘录）

- 前端：`MovieView.vue:148` posterUrl 模板调用两次+非空断言；`MovieView.vue:56` `Math.round` 与注释"向上取整"不一致；`statusCatalog.ts:26` 死代码；`ConfirmDialog` 无焦点陷阱（a11y）；GET 缓存把 `/tasks`、`/notifications` 写入 localStorage 15 分钟且登出不清空。
- 后端：`security.py:560-576` `redact_mapping` 死代码；`security.py:143,479` 速率窗口 dict 不清理；`api/subtitles.py:20` async 路由内同步执行 + `subtitle_names` 无单条长度上限；`api/library.py` 多处 detail 为裸英文错误码（违反中文文案约定）；`config.py:75` `cookie_secure` 默认 False；`migrations.py:618-646` 038 迁移引用 `details_json` 列（推测依赖旧表结构）；`tasks.py:294-321` `recover_after_restart` 死代码。
- STRM：`strm_playback.py:107-118` 全局锁内做 DB 往返串行化所有缓存命中；`strm_manifest.py:444-572` 默认同步路径不 retire 已删文件的 manifest/.strm（孤儿累积）；`strm_scope.py:144-153` 播放前缀未拒控制字符；`empty_directory_cleanup_plan.py:222` GET 端点却触发写库（recover_stale_applying）；`webhooks.py:436-453` SSRF DNS rebinding TOCTOU（当前用自定义 transport 绑定 IP 可根治）。
- 适配器：`tmdb.py:466-477` 429 `Retry-After` 无上限（NaN 会 ValueError）；`p115_c03_fixture_probe.py` 探测预算 4 与验证路径页上限 8 不一致；`prowlarr.py:232-233` 注入 client 时全局改写 X-Api-Key 头（共享 client 时密钥扩散）。
- 部署：`README.md:203` tailscale 命令对 Compose 内部 8000 端口（systemd 主部署是 8115）；`scripts/backup_db.ps1:18-20` 要求 40 位 SHA 但 compose 默认 `local`；`docker-compose.yml:36` P115_COOKIE_PATH 默认无卷挂载；`scripts/verify-live.sh:9` 硬编码 Windows 残留路径。

---

## 各子系统整体评估

- **后端核心/API**：写操作门禁、租约 fencing、日志脱敏、错误码中文映射、迁移幂等性均达生产级，未发现可直接远程利用的凭据窃取或越权 115 写漏洞。最系统性的弱点是 **security.py 路径→scope 映射对未知路径 fail-open**，会随代码演进逐渐侵蚀授权边界。
- **整理/库存**：fail-closed 链条完整且分层（gate+契约+确认+租约 CAS+写后置校验），扫描不完整绝不产生删除结论。**唯独自动清理路径（`_auto_clean_source`）绕过全部门禁**，是项目自述门禁的直接冲突，应优先修复。
- **STRM/任务/搜索**：STRM 安全教科书级（无敏感信息入 STRM、防穿越/符号链接、租约围栏、幂等补偿）；任务状态机 fencing 到位。最严重问题集中在"租约/认领/预算边界之外的兜底缺失"：`recover_incomplete` 误杀 QUEUED、worker 回收吞掉成功提交、webhook 重复 POST、搜索无总预算。
- **适配器**：凭据安全整体做好（无日志语句、异常链 `from None`、repr 脱敏、Prowlarr 同主机校验、qB 禁环境代理）。最严重是**永久删除身份碰撞防护在 size 缺失时可被绕过**（不可逆数据丢失），以及 990009 busy 重试误施于非幂等写。
- **前端**：错误目录、竞态防护、凭据脱敏、危险操作确认都相当扎实。最严重是**会话失效后无全局 401 处理**——自托管管理台在会话过期后停留在"已登录"假象，写操作静默失败。
- **测试/部署**：覆盖率高（1145 单测 + 231 前端全绿），但存在**可确证的集成测试回归**（`test_library_scan_scope_recovery.py` 2/3 失败，verify 门禁红）、Dockerfile 构建断裂、若干 fail-closed 关键分支零覆盖与 flaky 时序断言。

---

## 建议修复优先级

1. **P0（安全/数据安全，立即处理）**：自动清理绕过写门禁（#1）、永久删除身份碰撞绕过（#2）——前者直接违反项目门禁，后者是唯一不可逆数据丢失路径。
2. **P1（状态机/一致性，本周处理）**：recover_incomplete 误杀 QUEUED（#3）、worker 租约回收吞掉成功提交（#4）、订阅 check 复活已取消订阅、webhook 重复 POST、uncertain 计划失配卡死。
3. **P1（门禁/构建）**：Dockerfile config/ 修复 + 修复集成测试回归（`test_library_scan_scope_recovery.py`）+ 补 fail-closed 关键分支测试（p115_delete/删除端点/inventory_push_guard）。
4. **P2（架构）**：security.py scope fail-open 改 deny-by-default、QR 轮询改 POST、日志导出加限流。
5. **P3（前端体验）**：全局 401 处理、非 JSON 响应容错、轮询链式化。

**验证备注**：本次为只读审查，未修改任何文件、未执行任何 115 写操作或部署。当前工作树未提交改动仅含 `search.py` 注释更新与前端测试依赖（@testing-library/vue、msw），与上述回归无关。
