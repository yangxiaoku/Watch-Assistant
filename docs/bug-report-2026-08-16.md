# Watch Assistant 缺陷排查报告（2026-08-16）

排查对象：`192.168.6.236` 生产实例（release `e6eb6dd`，systemd 运行中）。
排查方式：服务器日志/数据库取证 + 只读 115 探测 + 本地代码走读与单测复现。
未做任何 115 写操作（见文末「风控约束与测试说明」）。

---

## 一、总体结论

**所有主要故障共享一个根因**：115 网盘对服务器环境的 `/files` 类端点进入 **HTTP 405 风控窗口**（约 8/16 04:50 起持续至今，实测仍 405），导致：

1. 媒体库树扫描全部失败（`gateway_error`）；
2. 推送守卫 fail-closed → **所有推送失败**（`inventory_index_incomplete`）；
3. 组织自动化读取源目录失败 → **整理无法开始**（`organize.automation.blocked`）。

叠加四个独立缺陷：订阅"自动推送完成"报告虚假成功、覆盖策略 `conflict_mode` 0/1 语义写反、订阅不按季/集确认已有内容、失败推送永不自动重试。另有配置指向测试目录、目录环检测残留、内容检测大量超时等次要问题。

---

## 二、问题清单（按严重度）

### 🔴 P0-1 推送全部失败：`inventory_index_incomplete`（媒体库库存扫描不完整）

**现象**：8/16 07:01 起所有推送任务失败。数据库统计（`tasks` 表）：

| 状态 | 数量 | 最近时间 |
|---|---|---|
| failed / `inventory_index_incomplete` | 40（其中 07:00 后 32） | 08-16 11:29 |
| failed / `inventory_index_stale` | 5 | — |
| available（成功入库） | 36 | 08-16 04:33 |
| uncertain | 34 | 08-16 03:57 |
| submitted（卡住） | 2 | 08-16 04:33 |

**根因链**（代码：`services/inventory_push_guard.py` + `worker.py` + `app.py:_refresh_inventory_before_push`）：

- 推送前守卫要求**所有 enabled 媒体库**都有「完整 + 新鲜 + 覆盖根目录」的树扫描快照，任一库不满足即 fail-closed 拒绝提交（`InventoryPushCheck(False, "inventory_index_incomplete")`）。
- 当前有 2 个启用库：
  - `main`（整理归档目录 `3482969620225197691`，snapshot rev 56，最近一次完整扫描 11:29 成功）；
  - `org-source-…`（整理源目录 = **测试115 `3482085898508567892`**，8/16 11:30 起每次扫描 `gateway_error`）。
- `org-source` 扫描失败 → 整个守卫不通过 → 所有推送失败，即使 `main` 快照完好。
- 扫描失败的底层原因：**115 `webapi.115.com/files` 与 `files/app` 端点对服务器返回 HTTP 405**。只读探测证据（2026-08-16 ~19:40，服务器本地、持久 Cookie、生产同款 transport/UA 画像）：

```
fs_files  (3482969620225197691) → HTTPError 405 Method Not Allowed
fs_files  (3482085898508567892) → HTTPError 405 Method Not Allowed
fs_info                        → HTTPError 405 Method Not Allowed
user_info                      → OK（账号本身未被封锁，是 /files 端点级风控）
```

**时间线（应用日志）**：02:41-02:43 库扫描成功 → 03:57 推送 31 条 **uncertain**（提交结果不明，风控前兆）→ 04:33 推送 30 条 **available**（最后成功）→ 04:50 起 `organize.automation.blocked gateway_error` → 07:01 起推送全部 `inventory_index_incomplete`。

**2026-08-16 晚实测（换账号尝试期间，逐层定位后的最终根因）**：

1. **判型 deadline 不足（主因，确定性失败）**：`P115ReadOnlyDirectoryGateway.list_directory` 对页内**每条记录**做一次 `fs_info` 判型（目录/文件），且每次真实请求前有进程级 `throttle_read`（0.5s）。org-source 根目录 38 条 → **39 次调用 × (0.5s 节流 + 请求往返) ≈ 38 秒**，超过 `request_timeout_seconds=30` 的 deadline → **`request_timeout` 确定性失败**（每次扫描 30s 后必挂，与 115 状态无关）。隔离实验证据：同一进程内 `transport.fs_info_app` 直调 0.29s 成功；`gateway.list_directory`（30s）15-30s 必失败；**`request_timeout_seconds=60` 后同一调用 38.3s 成功、38 条全部判型通过**。
2. **proapi 高频黑洞（加剧因素）**：39 次/页的 proapi 调用（`/android/` 路径）在高频连续请求下触发 115 黑洞（TCP 连接建立但无 HTTP 响应，30s 超时）；黑洞期**所有**进程（含独立脚本）的 proapi 请求都超时，冷却数十分钟后恢复；webapi 路径不受影响。`transport` 的 app→web 回退只处理「快速失败/结构化 405」，**超时不会回退** → 黑洞期整页失败。
3. **managed cookie 切换（12:01-12:23 失败期）**：11:46 用户在 GUI 扫码登录新账号（`p115_login_devices` 新增 active 设备）→ `CompositeCookieProvider` 优先用新账号 cookie → 新账号对旧账号的目录无权限 → 扫描失败；独立脚本用文件 cookie（旧账号）成功。已切回旧账号设备（`f70b4d16`，实测有效）。
4. **风控恶性循环**：失败后系统**无退避**地反复重试——10:39:56-10:40:51 出现 26 次 `inventory.refresh.failed`（约每 2.1 秒 1 次），30 个订阅任务两分钟内约 120 次 115 请求；网关 405 退避 45s × 2 次，窗口持续数小时时形同虚设；自动整理调度（120min）+ 多触发源（约 35-45 分钟一次）继续打 115。

**修复方向（按此轮实测）**：
- `request_timeout_seconds` 30 → 90s（覆盖大目录判型总量）；
- 判型节流 `READ_THROTTLE_SECONDS` 0.5 → 0.15s（与批量页 50 节奏匹配，总耗时降 ~3 倍）；
- proapi（fs_*_app）超时/黑洞时**回退 webapi** 而非失败；
- 单条判型失败降级（重试或跳过该条），不整页失败；
- 部署后重新启用 org-source 库、重跑推送验证。

### 🔴 P0-2 订阅自动推送「报告成功」但任务实际全部失败

**现象**：`subscription.auto_push_completed pushed=30 total=30`（10:36:14）——文案为「自动推送完成：成功 30 条，共 30 条」，但同刻创建的 30 个任务**全部** `failed / inventory_index_incomplete`。

**根因**（`services/subscriptions.py:_auto_push_resources`）：`pushed` 统计的是 `task_service.create()` **创建任务记录**的次数，不是推送执行结果。任务随后由 worker 异步执行并失败，订阅侧没有任何回读/校验，照常发出「完成」事件。用户界面会显示"推送成功"，实际 115 什么都没收到。

### 🔴 P0-3 覆盖整理 `conflict_mode` 0/1 语义写反（会导致误删大文件版本）

**代码**（`services/organization_policy.py:compare_versions`）：

```python
candidate_wins = (
    candidate.size_bytes > existing.size_bytes
    if policy.conflict_mode == 0          # 设置页文案：「小文件优先」
    else candidate.size_bytes < existing.size_bytes  # 「大文件优先」
)
```

**本地复现（实际运行结果）**：

| 设置 | 设置页文案 | 候选 vs 现有 | 实际裁决 | 期望 |
|---|---|---|---|---|
| `conflict_mode=0` | 小文件优先 | 1G vs 4G | `existing`（保留 4G） | 小文件赢 |
| `conflict_mode=1` | 大文件优先 | 4G vs 1G | `existing`（保留 1G） | 大文件赢 |

**生产配置恰为 `conflict_mode=1`（大文件优先）→ 实际执行的是小文件优先**：同名字幕/影片冲突时，大文件版本会被当成旧版本回收（移入 115 回收站），小文件留下。这是有破坏性的 bug。`tests/unit/test_organization_policy.py` 只覆盖 remux/tie/dolby/mode2 分支，**未覆盖 size_priority 分支**，所以反转未被测试发现。

另注：`multi_version_enabled`（杜比+非杜比共存）分支位于 `conflict_mode==2` 提前 return 之后，mode=2 时共存策略实际永不生效，与设置页允许同时勾选的表现不符（次要）。

### 🟠 P1-4 整理无法开始（+ 配置指向测试目录）

- 自动整理被阻断：`organize.automation.blocked gateway_error`「读取源目录失败」（源 = `3482085898508567892`），日志 8/16 04:50 起反复出现；
- 组织操作历史停在 8/14 14:37，之后无任何新计划/新执行；历史分布：organized 12 / uncertain 8 / failed 16（`plan_prerequisites_changed`）；
- **配置疑点**：`organization_settings_json` 中 `source_directory_ids` 与 **`push_directory_id` 都指向 `3482085898508567892`**（按 AGENTS.md 记载这是「测试目录，干净且只有一个 wav 文件」）。若该目录确为测试目录，则：整理源配置错误（整理永远扫不到真实内容）、且**推送目标目录也是它**（即使守卫放行，磁力/分享也会转存进测试目录而非归档目录）。需用户确认该目录身份；若为历史测试残留，应改回真实目录并重验 scope。
- 另：自动整理调度 `scan_interval_minutes=120` 但日志 blocked 约每 35-45 分钟一次（多触发源），失败轮询无退避，同样在喂养 405。

### 🔴 P0-4 订阅 AUTO 模式批量推送触发 115 风控（「订阅了之后就风控」的直接原因）

**现象**：用户在 08-16 03:28 创建 AUTO 模式订阅（`sub_69474d0e…`，tmdb 60625 整剧，无季号）后，当天 04:50 起 115 进入 405 风控窗口；10:36 第二轮推送后再次进入扫描风暴。

**因果链（DB/日志实证）**：

```
订阅检查命中 31 条资源（整剧订阅 → 搜索全部季 → S01-S09 多版本全命中，
如 Rick and Morty S01 同时有 x265-RARBG / x264-DAA / edge2020 三个版本）
→ _auto_push_resources 一次性创建 31 个推送任务（04:33:51，1 秒内）
→ worker 逐个处理：守卫共享同一新鲜快照（04:33:52 刷新 1 次后全部通过）
→ 04:36:39-04:42:33 连续提交 31 次 115 离线下载（写操作，约 3s/个）
→ 04:50 起 115 /files 端点 405 风控（08-16 实测：账号正常、端点级封锁）
→ 风控期后续任务失败（inventory_index_incomplete）
→ 每个失败任务触发一次全库刷新扫描 → 10:40 单分钟 23 次扫描（风暴）→ 风控持续
→ 10:36 第二轮 AUTO 推送 30 任务 → 27 个失败 + 扫描风暴
```

**量化证据**：04:33 轮任务 31 个全部 `available/submitted`（提交成功）；提交节奏约 3s/个、1.5 分钟内完成；`library_scan_runs` 在 10:40 一分钟内 23 条。对比：04:33 前推送一直正常（02:41/11:29 扫描成功、无 405），风控恰在「31 次批量离线下载提交」之后出现。

**放大器**：
- **整剧订阅（season_number=None）搜索全部季**，且**同季多版本资源不按季/集去重**（S01 三版本全推）→ 无谓任务数 3 倍；
- **无「该季/集是否已在库/在途」检查**（P1-5），重复内容也推；
- **失败任务无退避**：每任务触发一次全库刷新（`_refresh_inventory_before_push` 无条件扫描所有库，不先检查快照新鲜度）→ 风暴；
- `P115_MAX_CONCURRENCY=1` 只限制并发，不限制提交频率（0.5s 节流对写操作阈值太低）。

**修复方向**：
- AUTO 推送**按 tmdb_id+季/集去重**（同一内容只推最优版本）+ 跳过库存已存在；
- 单轮 auto_push **数量上限**（如 5 个/轮，余量下轮补）；
- 提交间隔加大（写操作 10s+）；`_refresh_inventory_before_push` 先查快照新鲜度、新鲜库跳过；失败任务对 inventory 类错误指数退避。

### 🟠 P1-5 订阅不按「季/集」确认已有内容，失败也不重试

- **无季/集已有性检查**：AUTO 模式对当轮搜索匹配的**全部**资源创建推送任务，去重仅按 `resource_id`（`TaskService.create` 幂等），不查询库存快照里该季/该集是否已存在（`_should_auto_pause` 的完整性评估只用于自动暂停，不参与推送过滤）；
- **整剧订阅（`season_number=None`，如 sub_69474d0e…，tmdb 60625）搜索全部季，命中即推**，与用户「按季和集确认是否有这个」的预期不符；
- **失败永不自动重试**：`_auto_push_resources` 注释明确「已存在的任务（含失败待重试）不会重复创建」——30 个任务失败后，16:36 下一次订阅检查对相同资源不会重建任务，用户看到"匹配 30 条"却永远不会再推；
- UI 层（`SubscriptionView.vue`/observations API）也只展示资源观测记录，无季/集粒度状态。

### 🟡 P2-6 `directory_cycle` 仍出现（9ea7eac 修复不完整）

- 8/16 11:29:02 `main` 库树扫描 `directory_cycle` 失败（重试后成功，间歇性）；
- 网关只过滤「子项 id == 当前目录 id」的自引用（`p115_library_gateway.py` 185-200 行），但 `_advance_tree_cursor`（`library_index.py` ~1923 行）对**任意已访问祖先**的 id 重现都判环——115 离线下载容器中的异常条目若 id 等于祖父/根目录仍会触发。

### 🟡 P2-7 内容检测大量超时/部分失败（workflow 卡在 partial）

- 8/15 以来：inspection item verified 1235 / timeout（`metadata_timeout`）597 / failed（`api_unavailable`）16；批次 completed 133 / partial 135 / failed 11；
- 直接后果：workflow inspection 阶段大量 `inspection_partial`（日志 8/16 11:16、11:27 等多处），并伴随「inspection batch finalize skipped workflow stage sync」告警（该告警本身是防回归的 fail-safe 路径，非 bug，但频率暴露了 inspection 失败面）；
- 8/15 17:49-17:55 曾连续 10 次整批 `metadata_timeout`。

### 🟡 P2-8 其它小问题

- `push_supported` 在 `app.py` 中**从未被置 True**（只有 `push_capabilities` 会被赋值），health 恒返回 `push_supported:false`；前端因优先读 `push_capabilities` 才未受影响，属潜在契约隐患；
- 2 个 `submitted` 任务（8/6、8/16 04:33 的 save_share）卡住——依赖 115 可读后由 `reconcile_submitted` 推进；
- 订阅表存在大量重复记录（tmdb 969681 ×6、27205 ×8 等，多为 cancelled）——若为用户重复创建则是交互问题，若为自动创建则需查证去重逻辑；
- `fs_files_app`（proapi）与 `fs_files`（webapi）**两个端点同时 405**，现有「app 端点优先、405 回退旧接口」策略失效，等于没有备用路径。

---

## 三、2026-08-16 晚实测结论（追加）

用户授权换账号/读写测试后，按「只读 → 推送 → 订阅 → 整理预览 → 夹具」顺序实测，结果：

| 步骤 | 结果 | 证据 |
|---|---|---|
| 只读探测 | ✅ 恢复（405 窗口解除） | fs_files count=38/39 正常，user_info 正常 |
| 手动推送 1 条磁力 | ✅ 全链路打通 | `task_8962b49f` submitted，remote_ref=infohash（115 受理） |
| 订阅检查 | ✅ 正常 | movie 603 匹配 44 条（remind 模式，不写 115） |
| main 库扫描（uvicorn 内） | ✅ 正常 | completed，49 条 |
| org-source 库扫描（uvicorn 内） | ❌ 30s 确定性失败 | gateway_error（根因见上：判型 deadline） |
| 整理预览 run-now | ❌ blocked | 源/目标目录读取 gateway_error（同根因） |
| 夹具整理执行 | ⛔ 未执行 | 受 org-source 扫描阻塞；夹具已上传（file_id 3496925172634486299）后删除（115 回收站） |

**测试期间临时变更（均已还原）**：临时 agent token（已吊销）；org-source 库临时 disabled（已恢复 enabled=1）；`auto_execute_enabled` 临时关闭（已恢复 true）；debug drop-in（已删除）；115 夹具（已删除）；**active 115 设备已从新账号切回旧账号 `f70b4d16`（7/31「这台电脑」，实测 cookie 有效）**——用户 11:46 扫码的新账号（`190f219b`）对现有目录无权限，**若确要使用新账号，需在新账号下重新配置源/归档/推送目录**。

**另注**：排查期间发现部署已推进到 `ade09672`（含 `5802d40`：push-inventory 批量页 50 修复——对应本报告 P0-1 的部分建议已上线）；`PROWLARR_SLOW_INDEXER_IDS` 当前为 `26,11`（AGENTS.md 记录的 10/18 已过时）。

## 四、风控约束与测试说明

- 已执行的最小只读探测（3 次调用 + 间隔）：确认 `/files`/`/info` 405、`user_info` 正常、账号未封；
- **未执行任何 115 写操作**：当前正处于 405 风控窗口，真实推送/整理/覆盖 E2E 必然失败，且每轮失败重试都在延长窗口；端到端验证应等窗口自然解除（建议停服 2-4 小时冷却，或换 IP/UA 验证）后，按「低频、间隔 >30s、单目录、先只读后单写」的方式补测；
- 建议在恢复后按以下顺序验证：① 只读 fs_files 单页 → ② 手动推送 1 条磁力到临时目录 → ③ 订阅检查 1 条 → ④ 整理预览（不执行）→ ⑤ 小夹具整理执行（受管目录）。

---

## 四、修复建议（按优先级）

1. **解除风控恶性循环**（P0-1 前提）：扫描/刷新/整理失败后指数退避（至少 5min 起），禁止 <10s 的连续重试；`inventory.refresh` 与订阅批量推送按任务串行节流；考虑 405 窗口期间暂停自动调度（组织/订阅/刷新）并给出状态提示。
2. **守卫范围可配置**（P0-1 根因之二）：`inventory_push_guard` 允许排除「组织源目录」类库（推送去重只依赖归档库快照），或将 org-source 从 `enabled` 库中分离，避免一个不可读的源目录锁死全部推送。
3. **修正 `conflict_mode` 反转**（P0-3）并补 size_priority 单测（两种 mode 各一例）。
4. **订阅推送结果回读**（P0-2）：`auto_push_completed` 改为统计「任务执行终态」或明确标注「已创建 N 条推送任务（待执行）」；任务失败后下一轮订阅检查应允许重建（按「失败且无成功记录」重建而非按 resource_id 幂等拒绝）。
5. **订阅按季/集去重**（P1-5）：推送前用库存快照 + TMDB 季集资料判断该季/该集是否已入库/已在途，已覆盖则跳过并记观察。
6. **配置核对**（P1-4）：确认 `3482085898508567892` 是否测试目录；整理源与推送目标应指向真实目录。
7. **目录环兜底**（P2-6）：网关过滤扩展到「子项 id ∈ 已访问祖先集合」或对 `directory_cycle` 增加「丢弃该目录子树并继续」的降级策略（受管范围内）。
8. 检查 inspection `metadata_timeout` 高频失败的根因（qBittorrent 探测超时预算/并发），减少 workflow partial。

---

## 五、防风控修复（2026-08-16 已部署 8336e21）

按用户优先级「不要风控」实施的修复（已合入 `codex/publish-main` 并部署，`POSTDEPLOY_RELEASE_CHECK=ok`）：

1. **订阅 auto_push 限流**：单轮新任务上限 5（余量下轮补）+ 按 tmdb/季/集内容去重（同内容多版本只推首个）；新增 `subscription.auto_push_limited` 事件。
2. **推送前刷新防风暴**：`library_scope_fresh` 只扫快照不新鲜的库（不打 115 的只读 DB 判断）+ 失败结果 120s 冷却缓存——批量失败任务不再逐任务全库扫描。
3. **写操作节流**：P115Adapter 离线下载提交/分享转存最小间隔 10s。
4. **判型 deadline**：gateway `request_timeout_seconds` 30→90s；transport 读节流 0.5→0.15s（与批量页 50 节奏一致）。
5. **proapi 黑洞回退**：proapi app 端点 8s 快失败预算，超时/405 回退 webapi——黑洞期不拖垮扫描。
6. （并行会话已上线 a055c83：扫描页间隔 1s + 组织调度失败退避上限 15 分钟。）

**部署后实测**：org-source 全树扫描从「第一页 38 条确定性失败」恢复为「可推进 414 条 / 57 目录 / 40 个目录树」；连续高频测试（探测 + 两次全树扫描 ≈ 千次调用）再次触发 115 风控窗口——确认 115 对调用量敏感，**生产常态（120min 一次扫描）调用量已下降约一个数量级，风控窗口内系统会退避等待**。

**遗留（整理相关，用户同意慢慢处理）**：P0-3 conflict_mode 反转、P2-6 目录环、P1-5 订阅按季/集确认（部分被 P0-4 内容去重覆盖）、P1-4 整理源目录配置核对、P2-7 inspection 超时。
