# 修复交接单（2026-08-11）

由审查会话产出，请工作会话按此清单修复。每条目含：位置、问题、建议修复方向、严重度。
修复完成后在 PR 描述里逐条标注"已修复/跳过(原因)"，审查会话负责复核。
完整审计记录见 `docs/reviews/bug-audit-20260811.md`。

---

## ✅ 已在本会话修复

### 本轮（2026-08-11 晚，分支 `codex/fix-other-bugs-frontend-20260811`）

| 编号 | 修复 | 验证 |
|---|---|---|
| H2/H3 | 注册迁移 072(订阅索引排除已取消行)+073(scan revision 唯一) | 迁移单测 22 过 + 新回归测试 |
| H1 | 永久删除身份碰撞:find_new_entries 稳定观察窗+多候选 UNCERTAIN | test_p115_delete 5 过 + 新碰撞测试 |
| H12 | retryable 服务不可用码 409→503 | 契约测试覆盖 |
| M2 | strm-manifest/cleanup-plans 补 strm scope 标记 | test_security 20 过 + 新断言 |
| H8 | qbittorrent 孤儿 torrent 按前缀识别清理 | test_qbittorrent 4 过 |
| H10 | qbittorrent trust_env=False 防凭据经代理泄露 | — |
| H4 | workflow 在途 PUSH 子任务时拒绝再推 | test_workflow_multi_child 3 过 |
| M20/M9 | p115 add_device 原子置位;revoke 404 | p115 测试 23 过 |
| M8/M11 | webhook_dns_failed 502;订阅 COMPLETED 不可复活 | 订阅测试 6 过 |
| M10 | inspection 超时 RUNNING 批次回收重排 | test_inspection_api 23 过 |
| M5 | db cleanup 保护未过期批次条目资源 | test_models 7 过 |
| M22 | dirty 租约丢失释放 STRM 操作 | test_directory_dirty_worker 9 过 |
| M24/B3/B4/B2/B5 | sw.js 离线 503、深链分页、搜索返回残留、loading 死码、skipped 标签 | vitest 233 过 |
| M25/M26 | SettingsView 二维码轮询与 save 系列卸载守卫 | vitest 过 |
| 前端优化 | MovieCard TMDB 圆形评分+hover 缩放、MovieRow 滚动按钮、详情 backdrop+星级+空简介、资源表平板卡片、设置页图标、tooltip/badge/skeleton 原语 | vitest 233 过 + build 过 |

### 遗留/委托（因并发会话占用或范围，本轮未动）

- **H7** p115 根目录目标任务可达性——audit-sec 会话在改 `p115_library_gateway.py`
- **H9** ~27 个非 catalog 错误码注册——机械量大，需后端+前端镜像+契约测试联动
- **H11** P115DeleteService 写门禁+审计——audit-sec 会话已在其分支实现
- **M3** org-history 默认 scope——与 org 会话边界重叠
- **M4** 后台 worker 监督——audit-sec 会话在改 app.py
- **M7** export_logs 无界——wa-fix-auto-clean 会话在改 settings.py
- **M21** 回收站分页 completeness 标记——与 H1 同文件，待 H1 合入后处理
- 全部 organization_* 与空目录清理项——归另一会话

### H0（已修复，独立提交 `62ccc13`）
- `scripts/acceptance_closure.py::_native_library_path`：打包内置原生库路径优先于 Homebrew。
  该测试此前失败导致 verify.sh 门禁恒挂。
- 清理分支 `codex/dead-code-cleanup-20260811` 另含 4 个提交（2 个后端死函数 + 6 个前端死方法 +
  2 处基线 ruff 违规），单测/集成/契约/vitest 全绿。

---

## 🔴 高危（安全/数据完整性，优先）

### H1. 永久删除身份碰撞 → 不可逆误删他人文件（建议按 P0 排期）
- 位置：`services/p115_delete.py:129-149` + `adapters/p115_permanent_delete_transport.py:113-144,179-193`
- 失败场景：删除目标仅靠 `(parent_id, name, size_bytes)` 匹配"回收站快照后新增条目"，`find_new_entries`
  首帧出现即返回；并发动作删入同目录+同名+同大小文件 → 碰撞条目被误判 → 对非目标执行不可逆 `recyclebin_clean`
- 修复方向：双观察窗口"持续唯一"才返回；任一帧 ≥2 候选即 UNCERTAIN；引入第二身份（大小+修改时间）
- 测试：注入"第 1 帧碰撞条目、第 3 帧真实条目"，断言等到真实条目或 UNCERTAIN

### H2. 迁移 070 唯一索引删重致活跃订阅丢失
- 位置：`migrations.py:766-786`（070）+ `_subscription_indexes_exclude_cancelled`（未注册进 MIGRATIONS）
- 失败场景：070 索引未排除已取消行，删重按 `MIN(id)` 保留 → "取消后重订"保留已取消行、删掉活跃重订行
- 修复方向：注册迁移重建带 `status != 'cancelled'` 的索引；删重优先保留非 cancelled 行
- 测试：构造"cancelled+active 同 scope"库升级后验证无 409、活跃行保留

### H3. 迁移 072 缺失：revision 唯一兜底失效
- 位置：`migrations.py:1391-1415`（`_unique_scan_run_revision` 未注册）
- 失败场景：`uq_library_scan_run_revision` 对已存在库不生效 → 并发完成扫描可写重复 revision
- 修复方向：注册为迁移 072
- 测试：旧库升级后验证索引存在、重复 revision 写入被拒

### H4. workflow 并发 PUSH 覆盖子任务绑定 → 首个任务终态静默丢失
- 位置：`workflows.py:1099-1106` + `tasks.py:552-562`
- 失败场景：同一 workflow 先后推送两个任务，PUSH 阶段 child 被覆盖，首个任务完成时抛 workflow_conflict
  → 变 UNCERTAIN 且 remote_ref 未持久化
- 修复方向：创建前检查未终态 PUSH 子任务并拒绝（或支持多子任务）
- 测试：并发两个 push 断言第二个被拒或首个不丢终态

### H5. STRM 运行中操作无法取消且错误码误导
- 位置：`strm_operations.py:426-435` + `api/strm.py:758-785`
- 失败场景：运行中长任务既不能取消、又因心跳续租永不回收
- 修复方向：专用错误码（对齐 task_not_cancellable）；或取消请求位
- 测试：运行中取消断言明确错误

### H6. 计划 select_candidate 无 CAS → 并发选择静默丢失
- 位置：`organization_plan.py:907-910/668-670`
- 失败场景：同 expected_revision 并发 select_candidate 都通过读检查 → 后者覆盖前者；plan_hash 失配致整计划报废
- 修复方向：CAS UPDATE（`WHERE id AND revision==expected`），rowcount≠1 抛 stale_revision
- 测试：并发两个 select_candidate，断言一个抛 stale_revision

### H7. p115 根目录目标任务永不可达 AVAILABLE
- 位置：`p115.py:368-379` + `p115_library_gateway.py:78-82/186-193/558-564`
- 失败场景：`target_cid=0` 合法配置，但网关不传 `allow_virtual_root=True` → get_status 恒 UNCERTAIN
- 修复方向：根目录目标传 `allow_virtual_root=True`；get_directory_detail 接受 "0"
- 测试：默认网关根目录路径提交后能观察 AVAILABLE

### H8. qbittorrent 孤儿 torrent 永久失败
- 位置：`qbittorrent.py:259-399/537-555/596-626`
- 失败场景：清理失败遗留无标记同 infohash torrent → 恒 ownership_conflict 且永不清理
- 修复方向：固定前缀 category + 按前缀回收孤儿
- 测试：崩溃后孤儿回收用例

### H9. 大量非 catalog 错误码直接上抛（前端无文案）

> **已修复**（分支 `codex/h9-error-codes`，2026-08-12）：36 个缺失码注册进
> `api_errors._CATALOG`（中文文案）+ `_HTTP_STATUSES`（状态映射），前端
> `errorCatalog.ts` 镜像（ADDITIONAL_CODES + CATALOG 特定文案）；契约测试
> `test_h9_registered_error_codes_have_specific_chinese_copy` 每码断言。
> `confirm_organization_plan` 的 `plan_hash` NULL 场景判空后再
> `compare_digest`（不再 TypeError 500），回归测试
> `test_confirm_with_null_plan_hash_raises_mismatch_not_500`。
- 位置：`strm.py:383/229/938/997/918-924/977-983/1088-1094/774-775/886-887`、`library.py:970-971/210`、`webhooks.py:131`、
  `settings_p115.py:251-255/303/315`、`organization_plan.py:109-112`（plan_hash NULL TypeError 500）
- 涉及码：strm_operation_claim_conflict/lease_required/not_running/not_reconcilable/creation_conflict、
  strm_full_failed/strm_incremental_failed/strm_cleanup_failed、target_*（15 个）、webhook_endpoint_disabled、
  invalid_poll_interval、invalid_cursor/invalid_limit、device_unavailable/invalid_device_code/invalid_device_name、
  invalid_small_file_threshold/target_file_invalid
- 修复方向：补 `_CATALOG` 或归一到已定义码（完整清单见审计报告"错误码 triage"）；plan_hash 判空再 compare
- 测试：每个新码一条断言

### H10. qb/tmdb/pansou httpx 默认 trust_env 泄露凭据
- 位置：`qbittorrent.py:114`、`tmdb.py:101`、`pansou.py:77`（prowlarr 已显式 trust_env=False）
- 失败场景：部署环境配置代理且未排除 localhost → qb 登录凭据/tmdb api_key 经代理转发
- 修复方向：依据部署意图设 trust_env=False 或显式代理白名单
- 测试：无代理时不受影响回归

### H11. P115DeleteService 无写门禁 + 永久删除无审计
- 位置：`p115_delete.py` + `api/library.py:1066-1089`
- 失败场景：绕过 API 直接调服务无门禁；不可逆删除不留审计痕迹
- 修复方向：服务层加 gate 参数（默认关闭）；端点成功/UNCERTAIN 分支统一 audit
- 测试：端到端断言删除产生审计事件

### H12. retryable 服务不可用码落 409 应 503
- 位置：`api_errors.py:_HTTP_STATUSES` + `organization_operation.py:444-463`/`organization_plan.py:236-239`
- 涉及码：operation_unavailable、organization_plan_unavailable、organization_reconciliation_unavailable、directory_ownership_unavailable
- 修复方向：`_HTTP_STATUSES` 补 503
- 测试：断言 503 + retryable

---

## 🟠 中危

### M1. 迁移 071 旧唯一约束 auto-index 不可见，约束残留
- 位置：`migrations.py:802-818`；修复方向：按 `get_unique_constraints()` 重建表或确认旧声明确为 `Index(unique=True)`
### M2. `/strm-manifest`、`/strm-cleanup-plans` 未入 strm scope 标记集 → 仅 strm:read token 无法读清单
- 位置：`security.py:602-611` + `api/strm.py:690`；修复方向：标记集补 `/strm-manifest`、`/strm-cleanup-plans`
### M3. `/api/v1/organization-history` GET 落默认 system:read，含敏感目标路径
- 位置：`security.py:632`；修复方向：映射为 `organize:plan`
### M4. 后台 worker fire-and-forget 无监督/重启
- 位置：`app.py:1236-1304`；修复方向：add_done_callback 记录崩溃并重调度
### M5. 完成批次保留期内引用过期资源 → 清理事务整体回滚
- 位置：`db.py:126-142` + `models.py:339-343`；修复方向：终态批次的 resource 纳入保护
### M6. `stop_organization` 未捕获 SettingsConflict → 500
- 位置：`api/settings.py:536-541`；修复方向：捕获 → 409 settings_conflict
- ✅ 已修复（`codex/m-series-fixes`）：包 try/except SettingsConflict → 409 settings_conflict；回归 `test_stop_organization_catches_settings_conflict_not_500`
### M7. `export_logs` 无界循环拉全量日志
- 位置：`api/settings.py:596-657`；修复方向：加总记录数上限或流式
### M8. `webhook_dns_failed`（retryable）映射 422 应 5xx
- 位置：`api/webhooks.py:132`；修复方向：422→502/503
### M9. `device_not_found` 硬编码 409 应 404
- 位置：`api/settings_p115.py:310-315`；修复方向：按 error_status() 分派
### M10. inspection 批处理非重启场景可永久卡 RUNNING
- 位置：`inspection.py:324-337/482`；修复方向：回收 RUNNING 且超时的批，或异常路径置回 QUEUED
### M11. subscriptions COMPLETED 死终态
- 位置：`subscriptions.py:138-142/165-170`；修复方向：补产生路径或从终态集合移除
### M12. organization retry 不检查 active operation → IntegrityError 裸 500
- 位置：`organization_operations.py:1326-1368`；修复方向：retry 前检查；捕获映射 operation_plan_conflict
- ✅ 已修复（`codex/m-series-fixes`）：retry 前 `_plan_has_active_operation` 检查 → operation_plan_conflict；回归 `test_retry_rejects_when_plan_has_active_operation`
### M13. ORGANIZING 取消不校验 lease token
- 位置：`organization_operations.py:1296-1307`；修复方向：CAS 置位或校验 token
- ✅ 已修复（`codex/m-series-fixes`）：ORGANIZING 取消改原子条件 UPDATE（status+revision CAS），冲突返回 operation_revision_changed/not_cancellable；回归 `test_organizing_cancel_is_atomic_cas_on_revision`
### M14. refresh_plan 失效化非 CAS → revision 累加丢失
- 位置：`organization_plan.py:518-521`；修复方向：统一走 _transition_plan CAS
- ✅ 已修复（`codex/m-series-fixes`）：refresh_plan 两处失效化改走 `_transition_plan` 原子 CAS；回归 `test_refresh_invalidation_is_atomic_cas_on_revision`（并发 gather 断言 revision 无覆盖丢失）
### M15. 手动"立即整理"硬编码 lease_active=True
- 位置：`app.py:726` + `organization_directory_provisioner.py:70-73`；修复方向：由操作表 lease 状态派生
- ✅ 已修复（`codex/m-series-fixes-2`）：`lease_active` 由 `summary.status is ORGANIZING` 派生，非 ORGANIZING 时 provisioner fail-closed（organization_lease_required）；回归 `test_directory_provisioner_lease_gate_fails_closed_when_not_organizing`
### M16. directory_dirty_worker run_forever 无异常隔离/退避 → 消费循环死亡
- 位置：`directory_dirty_worker.py:405-414`；修复方向：try/except + 指数退避
- ✅ 已修复（`codex/m-series-fixes`）：run_once 异常隔离 + 指数退避（上限 30s，与 organization_worker 一致）；回归 `test_run_forever_survives_run_once_exception`
### M17. library_index 并发完成写同 revision → IntegrityError 误标 FAILED
- 位置：`library_index.py:161/805-809`；修复方向：CAS 分配 revision + 库级锁
- ✅ 已修复（`codex/m-series-fixes-2`）：revision 分配改原子 UPDATE 子查询（MAX+1），SQLite 写锁串行化并发事务，跨进程不再撞唯一索引；回归 `test_concurrent_completions_allocate_distinct_revisions`（gather 两个独立 service 实例）
### M18. 库 root 变更后旧 root 迟到 run 抢更高 revision → 快照判非最新，库操作停摆
- 位置：`library_index.py:886-903` + `strm_scope.py:76-84`；修复方向：latest_revision 加 root_directory_id 过滤
- ✅ 已修复（`codex/m-series-fixes-2`）：`source_snapshot_is_current` 的 latest_revision 按 source run 的 root 过滤。revision 分配保持 library 全局（与唯一索引 `uq_library_scan_run_revision (library_id, snapshot_revision)` 一致，未改 root 作用域）；`current_snapshot_is_unique` 保持 library 全局（避免削弱跨 root 歧义检测）。回归 `test_source_snapshot_is_current_ignores_old_root_late_run`
### M19. empty_directory_cleanup create_plan 并发双击裸 500
- 位置：`empty_directory_cleanup_plan.py:194-215`；修复方向：捕获 IntegrityError 回滚重查
- ✅ 已修复（`codex/m-series-fixes`）：create_plan commit 捕获 IntegrityError → 回滚重查返回已存在计划；回归 `test_create_plan_handles_concurrent_plan_hash_conflict`
### M20. p115_login_devices add_device 非原子 → 双活跃设备
- 位置：`p115_login_devices.py:68-75`；修复方向：复用 mark_active 单条 UPDATE 模式
### M21. 回收站 10,000 条静默截断无完整性标记 → 永久删除恒 UNCERTAIN / SUCCESS 失真
- 位置：`p115_permanent_delete_transport.py:74-111`；修复方向：list_entries 返回 completeness 标记，超窗判 UNCERTAIN
- ✅ 已修复（`codex/m21-recycle-truncation`）：list_entries 达 max_pages 上限时返回 None（while...else 区分截断 vs 完整），调用方 fail-closed 判 UNCERTAIN；回归 `test_recycle_bin_listing_truncation_fails_closed`
### M22. dirty 租约丢失不释放已 claim 的 STRM 操作 → 阻塞最长 30 分钟
- 位置：`directory_dirty_worker.py:300-301/351-353`；修复方向：dirty_lease_lost 分支先 _fail_operation
### M23. recycle 动作不入 history
- 位置：`organization_operations.py:1239-1240`；修复方向：recycle action 也写 history
- ⏭ 暂缓（2026-08-12）：计划 `actions_json` 的 move action **不携带** `replacement_object_id`（`_execution_payload` 只校验不入 payload），回收是 executor 运行时按 `step.replacement_object_id` 的决定；`_record_history` 只读 `actions_json`，无法感知回收。需把 executor 运行时回收决策穿过完成事务（改动大，危及 N+1 优化过的历史完成事务）。保持现状，历史记录主 move，回收以 `plan_prerequisites_changed`/执行证据间接可见。
### M24. 前端：sw.js 离线 API 回退返回 index.html 被当成功空响应
- 位置：`frontend/public/sw.js:47-51` + `api.ts:735-736`；修复方向：API 路径无缓存返回 503/JSON 错误；ApiClient 校验 Content-Type
### M25. 前端：SettingsView P115 二维码轮询卸载竞态
- 位置：`SettingsView.vue:368-396`；修复方向：await 后检查 settingsMounted
### M26. 前端：SettingsView save/load 异步回调无 mounted 守卫
- 位置：`SettingsView.vue:564/757/848/912`；修复方向：统一加 `if (!settingsMounted) return`

---

## 🟡 低危（可批量）

- **L1** `manual_import.py:38-39`：resource_conflict 422 应 409 — ✅ 已修复（`codex/l-series-fixes`），回归 `test_confirm_import_resource_conflict_maps_to_409`
- **L2** `pwa.py:51-55`：pwa_device_conflict 422 应 409 — ✅ 已修复，回归 `test_pwa_http_error_maps_device_conflict_to_409`
- **L3** `search.py:193`：page 无上限，建议 le=10000；`search.py:198-199` 重复校验死代码 — ✅ 已修复 page 加 `le=10000`；**保留** page_size 显式校验（实测 Query enum 不拦截 page_size=30，非死代码，回归测试锁定 422）
- **L4** `tmdb.py:466-478`：Retry-After 无上限、page 无正整数校验；`tmdb.py:437-498` 无响应体上限 — ✅ 已修复（`codex/l-series-fixes-2`）：Retry-After 上限 30s、page 正整数校验、响应体上限 10MB
- **L5** `prowlarr.py:418-440`：downloadUrl 未校验路径前缀；`prowlarr.py:457-465` 兜底 GET 无响应体上限 — ✅ 已修复：downloadUrl 必须落在 `/dl/` 或 `/download` 下载路径（防同 host SSRF + API Key 泄漏）、兜底 GET 响应体上限 1MB
- **L6** `p115.py:282-293`：get_status 超时不丢弃 client；串行 101 页
- **L7** `cli.py:784`：strm generate `--full` 死参数 — ✅ 已修复（去掉 `required=True`，参数兼容已文档用法；handler 本就按 strm_command 分派）
- **L8** `config.py:63-65`：script_token_hash 全局全权限；`config.py:75` cookie_secure 默认 False；`security.py:457-485` 限流覆盖不全
- **L9** `models.py:751-753`：WorkflowEvidence 级联删除破坏证据链；`library_models.py:67-69` 库删除被扫描记录阻塞
- **L10** 死代码：`media_classification.plan_batch`、`media_matcher.MatchStatus.AUTO_ACCEPTED`、`strm_operations._commit`、`p115_library_gateway.MAX_SCOPE_VERIFICATION_PAGES`（前端 `PwaDevice` 类型）
- **L11** 无界内存：`_CLAIM_LOCKS`（strm_operations.py:48）、`_search_locks`/`_resource_search_tasks`（search.py:191-198）、`_locks`（season_metadata.py:39）
- **L12** `notifications.py:172-204`：通知去重 TOCTOU；`webhooks.py:261-284` publish_due 无原子认领；`workflows.py:855-866` workflow_id NULL 去重失效
  - webhook 部分 **已解决**：`_deliver` 已有原子认领（条件 UPDATE status==pending→in_flight + rowcount 检查），并发 publish_due 不会重复 POST。
  - notifications/workflow 部分 **暂缓**：需唯一约束（dedupe_key / 非 NULL workflow_id）+ 迁移，影响仅为重复通知/证据行（L 级低危），风险/收益不划算。
- **L13** 前端：`App.vue:1061-1068` 超时后来源/缓存摘要不更新；`App.vue:1628-1629` 推送能力告警不对称；`polling.ts:30` pollUntil 默认 isDone 陷阱；B2-B5（loading 死分支、深链分页夹紧、returnToBrowse 残留、strm skipped 标签）
- **L14** `app.py`：后台 worker 启动失败无告警（与 M4 同源）；`organization_scheduler.py` `_manual_runs` 无界、run_forever 无异常隔离 — ✅ 部分修复（`codex/l-series-fixes`）：`_manual_runs` 设上限（32）+ run_forever 异常隔离/指数退避；回归 `test_manual_run_queue_is_capped`、`test_run_forever_survives_run_once_exception`。app.py worker 启动告警与 M4 同源，归 audit-sec 会话
- **L15** `managed_directory_ownership.py:252-280`：_transition 无 CAS — ✅ 已修复（`codex/l-series-fixes-2`）：原子 CAS 迁移 + 并发幂等返回，回归 `test_concurrent_transitions_do_not_lose_revision`。`organization_history.py:76-87` OFFSET 分页无留存清理、`organization_executor.py:810-818` 历史 source/target 取首个成员 — **暂缓**：留存策略（按时间/条数）与多目录移动的历史归属是产品设计决策，L 级低危

---

## 修复纪律

1. 每个修复独立提交，PR 描述逐条对应上述编号
2. 修复后必须：受影响单测 + ruff + 前端 vitest 全绿；涉及迁移的加旧库升级回归
3. 高危（H1-H12）加回归测试
4. 中文注释/错误文案
5. 迁移类修复必须向前、幂等、可由发布前备份回滚
6. 工作树冲突：发布分支工作树勿与其他会话并发编辑；建议在独立分支 worktree 修复
7. verify.sh 门禁已恢复可用（B1 修复），合并前必须全绿

---

## 补充说明

- 完整审计记录（含错误码全表、死代码审计、文档审计）见 `docs/reviews/bug-audit-20260811.md`
- 本轮已删除的死代码见清理分支 `codex/dead-code-cleanup-20260811`（待合入发布基线）
- 文档过期点修复见文档分支 `codex/docs-audit-20260811`（待合入）
- 6 个映射活跃需求的"未接线规划模块"（anime_mapping/storage_governance/library_health/episode_coverage/media_technical/organization_isolation_contract）按用户决策保留不删
