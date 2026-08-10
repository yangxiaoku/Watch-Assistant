# 产品级改进与测试报告

- **日期**:2026-08-10(三轮修复:第一轮 14 项 + 第二轮 7 项 + 第三轮 2 项 P1)
- **基线**:`5068e8f` → 已发布三轮:`2e62821` → `6316299`(当前部署)
- **测试环境**:部署实例 `http://192.168.6.236:8115`(Docker),桌面 1440×900 与移动 390×844
- **方法**:全量只读代码审查(5 个子系统并行)→ 按严重等级修复 → 单元/集成回归 → 真实用户路径 Playwright 浏览器验收

---

## 1. Bug 挖掘成果

对全部后端(约 66K 行 Python,API/Services/Adapters/Core)与前端(Vue 3 + TS)做了系统性只读审查,
共确认 **61 条真实缺陷**,按严重等级分布:

| 等级 | 数量 | 说明 |
|---|---|---|
| P0 | 0 | 未发现凭据泄露、SQL 注入、路径穿越、STRM 嵌 Cookie 等致命问题 |
| P1 | 13 | 核心流程故障/安全边界削弱/确定性失败 |
| P2 | 26 | 次要功能故障/恢复路径缺失/一致性破坏 |
| P3 | 22 | 边缘瑕疵/防御纵深 |

> 三轮修复合计 **23 项(P1×10 / P2×5 / P3×8)**,覆盖全部 13 条 P1 中的 10 条。

按子系统分布:Services 15 条、Adapters 13 条、Core 12 条、Frontend 12 条、API 9 条。

> 审查过程中发现 1 条代理误报(`normalize.py` 合并方向),被既有回归测试
> `test_duplicate_infohash_keeps_plugin_metadata_but_structured_fields_win` 排除——
> 该处语义(结构化字段胜出)本就是测试锁定的设计,未改动,仅补充注释。

---

## 2. 已修复 Bug 清单(13 项)

按严重等级从高到低。全部修复均通过现有/新增单元测试与前端构建验证。

### P1 修复(6 项)

| # | 位置 | 问题 | 原因 | 修复方式 |
|---|---|---|---|---|
| 1 | `services/media_parser.py` | 年份紧跟季集标记时被 end 组吞成"集数范围"(`Show.S01E01.2019.mkv` → ep=1-2019),自动匹配永久失败 | end 组允许纯数字且无界 | 有界校验 `start < end <= start+200`,误匹配时丢弃 end 并把掩码收缩到 end 组之前,年份可继续提取;end 组同时支持 `S01E05`/`S02E03` 跨季标记,标题不再残留后半段标记 |
| 2 | `api/library.py` | `configure_library`/`verify_library_scope` 缺 `_allowed` 校验,受限 agent token 可越权改其他媒体库 | 与库级其他写路由不一致,漏加范围校验 | 两个 handler 入口补 `_allowed(context, library_id)` 检查(404) |
| 3 | `db.py` | SQLite 无 `busy_timeout`,写锁竞争(worker 心跳/agent 计数/任务推进)下随机 "database is locked" 500 | 默认 busy_timeout=0,遇写锁立即失败 | connect 事件加 `PRAGMA busy_timeout=10000`,写竞争时等待而非崩溃 |
| 4 | `adapters/qbittorrent.py` | 元数据缺失的既有 torrent(他人添加,stoppedDL/error/missingFiles)被误报 VERIFIED,空文件清单当已验证证据 | `_inspect_existing`/新入库分支未检查 `has_metadata` | 两个分支在汇总前强制 `_has_metadata(torrent)`,缺失按 `metadata_stop_failed` 处理 |
| 5 | `services/subscriptions.py` | 搜索期间被取消的订阅会被旧任务改回 MATCHED 复活并继续排程 | 搜索返回后无条件覆盖 status/next_check_at | 完成路径重读订阅,终态(PAUSED/CANCELLED/COMPLETED)时抛 `subscription_not_active`,不写状态不排程 |
| 6 | `worker.py` | `run_forever` 异常路径 `sleep(0)` 热循环,DB 故障时满速刷查询刷日志 | 无退避 | 指数退避 1s→30s,成功复位 |

### P2/P3 修复(6 项)

| # | 位置 | 问题 | 修复方式 |
|---|---|---|---|
| 7 | `services/library_scan_scheduler.py` | 当日调度扫描失败后当天永不重试,快照全天 STALE,推送 fail-closed | FAILED 运行在 30 分钟冷却期后重入队(重置回 QUEUED);RUNNING/CANCELLED/已完成的跳过 |
| 8 | `frontend/.../LibraryWorkbenchView.vue` | STRM 轮询中切换媒体库 → 挂起的 `waitForOperationPoll` 永不 settle → `busy` 永久死锁,工作台全部按钮禁用 | 轮询等待改为可取消:generation 变更时立即 settle 全部挂起等待(新增 `bumpOperationPollGeneration`,统一三处调用点) |
| 9 | `adapters/prowlarr.py` | 分页偏移按整页长度推进,overfetch(1337x ~80 条)时每页静默丢一批结果 | 偏移按实际消费条数 `len(page_items)` 推进 |
| 10 | `services/validation.py` | seeders/size 未知的资源排序取反后排最前,默认推荐/推送取错资源 | None 映射为负无穷,降序垫底 |
| 11 | `frontend/.../App.vue` | 登录按钮无禁用态,弱网双击并发提交触发限流误报 | 增加 `loggingIn` 状态:按钮禁用 + spinner + "登录中…" |
| 12 | `frontend/.../SettingsView.vue` | P115 设备「移除」无确认框,误点即注销设备 | 复用 ConfirmDialog(danger 风格,双确认门禁) |
| 13 | `frontend/.../LibraryWorkbenchView.vue` | 媒体库工作台明文展示 `root_directory_id`,与设置页脱敏策略不一致 | 与 SettingsView 一致的 `redactDirectoryId` 脱敏展示 |
| + | `adapters/p115_playback_gateway.py` | `from error` 链起底层异常,可能把凭据细节带进 traceback/日志 | 改 `from None`,与全库脱敏约定一致 |

> 注:#7 之前(前端)与 #12 之外,另有 `db.py`/`worker.py` 等改动属于可靠性增强,见变更清单。

---

### 2.2 第二轮修复(7 项,2026-08-10 追加)

| # | 位置 | 问题 | 修复方式 |
|---|---|---|---|
| 14 | `services/webhooks.py` | 密钥轮换/损坏时解密异常中断整批投递,该行永久队头阻塞,所有端点饿死 | 解密包 try/except 转 `_record_delivery_failure("secret_unavailable")`(退避重试,超限转 dead);`publish_due` 单行异常不中断整批;新增回归测试 |
| 15 | `services/tasks.py` | 对已取消任务 reconcile 会把状态改回 SUBMITTED/FAILED 复活 | reconcile 入口对 CANCELLED 拒绝(`task_not_reconcilable`);`apply_remote_status` 增加 CANCELLED 终态守卫(覆盖 worker 竞态);新增集成回归测试 |
| 16 | `services/mcp.py` | library 受限 token 可越权读取全部任务/通知/工作流(非库维度实体,无法按库过滤) | `_require_unscoped`:受限 token 读取这些全局实体 fail-closed(`resource_forbidden`);resources URI 与 tools 双路径覆盖;新增回归测试 |
| 17 | `services/p115_login_devices.py` | 并发激活设备可致零活跃(凭据丢失)或双活跃(active_cookie 不确定) | `mark_active` 改单条原子 UPDATE(置位+清扫一体);`active_cookie` 加确定性排序;新增并发回归测试 |
| 18 | `adapters/p115_permanent_delete_transport.py` | 回收站列表只读前 100 条,超窗新记录永远找不到 → 永久删除恒 UNCERTAIN | `list_entries` 分页遍历(100 条/页,100 页上限保护);新增跨页回归测试 |
| 19 | `services/library_scan_operations.py` | 网关 CANCELLED 无限 requeue 热循环(每秒全量重读),扫描永不完成 | 重排队次数上限 `MAX_SCAN_REQUEUE_ATTEMPTS=5`,超限转 FAILED(`scan_requeue_limit`,含中文文案);与调度器冷却重试协同;新增回归测试 |
| 20 | `adapters/tmdb.py` | 429/5xx 无分类无退避,批量富化必触限流且静默降级 | 新增 `TmdbRateLimitedError`(携带 Retry-After);`_get` 有界重试 3 次、指数退避、尊重 Retry-After;401/403/404 不重试;新增 3 个回归测试 |

### 2.3 第三轮修复(2 项 P1,2026-08-10 追加)

| # | 位置 | 问题 | 修复方式 |
|---|---|---|---|
| 21 | `adapters/p115.py` | 写/状态路径裸 `asyncio.to_thread` 无超时:115 侧挂起时线程永不返回,worker 续租永续 → 任务永久卡 SUBMITTING 且流水线停摆;990009(服务端仍在处理上次提交)无重试,直接 UNCERTAIN | `_call` 统一 `asyncio.wait_for`(默认 30s,`request_timeout_seconds` 可配置);990009 按文档幂等重试一次(3s)拿确定性结果;超时不重试(避免重复副作用)映射 `UNCERTAIN timeout` 优先走只读核对;`get_status` 分页与两个凭据探针同样有超时上限;回归测试 +3 |
| 22 | `services/organization_plan.py` | 目标分类子目录缺失时计划回退到目标根(provisioner 仅创建子目录供后续归档),但 `target` 仍显示完整分类路径 → 用户按路径找不到文件,预览/确认/历史与执行不一致 | fallback 条目的 `target` 按实际落点(目标根下的文件名)生成,与 `target_parent_id`、执行、历史记录四处一致;回归测试 +1 |

## 3. 真实用户路径浏览器测试(Playwright)

针对**真实部署实例**编写 `frontend/e2e/live/**`(凭据仅经环境变量注入,不落仓库),配置
`frontend/playwright.deploy.config.ts`(setup 项目真实 UI 登录 → storageState 复用)。

### 3.1 结果总览:**18 / 18 通过**(桌面 13 + 移动 2 + 未登录 2 + 登录 setup 1)

| 项目 | 用例 | 结果 |
|---|---|---|
| no-auth | 未登录访问被登录门拦截 | ✅ |
| no-auth | 错误密码显示中文错误提示(凭据错误/限流/锁定三选一,不泄露细节) | ✅ |
| setup | 真实 UI 登录并保存会话 | ✅ |
| desktop ×13 | 首页发现板块、电影目录+翻页(URL=2 且内容变化)、剧集目录、热门、顶栏搜索→详情、详情资源区+质量筛选、整理工作台(状态/待处理/历史)、媒体库工作台、任务中心+推送抽屉开合、通知中心、日志页、设置全 7 分区、收藏与记录 | ✅ |
| mobile ×2 | 首页/目录→详情无横向溢出(桌面与移动项目双跑) | ✅ |

部署前后多次完整重跑全部稳定通过,无 flake。

### 3.2 截图(15 张,位于 `frontend/test-results/deploy/screenshots/`,测试报告页面内嵌)

| 文件 | 场景 |
|---|---|
| `01-login-gate.png` | 未登录登录门 |
| `02-login-wrong-password.png` | 错误密码中文提示 |
| `03-home.png` | 首页发现板块 |
| `04-movies-catalog-p2.png` | 电影目录第 2 页 |
| `05-tv-catalog.png` | 剧集目录 |
| `06-movie-detail.png` | 影片详情 |
| `07-detail-resources.png` | 详情资源区 |
| `08-organization-status.png` | 整理工作台状态视图 |
| `09-library.png` | 媒体库工作台 |
| `10-workflows.png` | 任务中心 |
| `11-notifications.png` | 通知中心 |
| `12-logs.png` | 日志页 |
| `13-settings-overview.png` | 设置概览 |
| `m01-home-mobile.png` / `m02-detail-mobile.png` | 移动端首页/详情 |

### 3.3 测试过程中的真实发现

- 首轮 17 个用例失败,全部为**测试定位器与实际 DOM 不符**(评分混入标题、按钮重名、
  分区标签名错误、/library 实际渲染工作台而非目录),非应用缺陷;修正后全绿。
- 过程中核实部署端行为:后端分页数据正确、设置 7 分区渲染完整、CSRF/会话机制按设计工作。

---

## 4. 测试覆盖率概览

| 层 | 命令 | 结果 |
|---|---|---|
| 后端单元+集成 | `.venv/bin/python -m pytest tests/unit tests/integration` | 全部通过(见提交说明) |
| 前端单元 | `npm --prefix frontend test -- --run` | 29 文件 / 226 用例 ✅ |
| 前端构建 | `npm --prefix frontend run build` | ✅ |
| 真实部署 e2e | `npx playwright test -c playwright.deploy.config.ts` | 18/18 ✅(部署前后多轮稳定) |
| 新增回归测试 | `test_media_parser.py`(+7 参数化)、`test_library_scan_scheduler.py`(+1) | ✅ |

---

## 5. 仍存在的风险与后续建议(未修复项)

以下为已确认、本次未实施修复的高价值项,修复方向已由审查给出:

| 优先级 | 位置 | 风险 | 建议修复方向 |
|---|---|---|---|
| P1 | `adapters/p115.py` | 写/状态路径无超时与 990009 重试,单次挂起可拖死 worker;get_status 网络错误与"任务不存在"不可区分 | 写调用统一带 `asyncio.wait_for`;990009 幂等重试一次;RemoteObservation 区分 error_code |
| P1 | `services/organization_preview.py` + `executor` | 目标分类子目录不存在时预览显示子目录路径、实际落到根目录(首轮整理) | 执行前把冻结路径重新解析为真实子目录 ID,或回退场景降级为 review 并明示根目录 |
| P1 | `services/strm_manifest.py` | 同目录"删除旧+同名新增"一次扫描窗口内 `path_collision`,全量模式永久失败 | diff 分两趟:先 retire removed,再生成 added/modified |
| P1 | `services/webhooks.py` | ✅ 已修复(2.2 #14) |
| P1 | `services/library_scan_operations.py` | ✅ 已修复(2.2 #19) |
| P2 | `api/settings_p115.py` | 目录浏览原地改写全局白名单,可绕过推送目标限制;白名单不持久 | 浏览集合按会话隔离,推送/整理目标显式"确认选定"并持久化 |
| P2 | `services/library_index.py` | 快照 revision 并发重复分配 → 消费者 fail-closed | 完成点单条原子 UPDATE 分配 revision,或按 library 串行化 |
| P2 | `services/p115_login_devices.py` | ✅ 已修复(2.2 #17) |
| P2 | `frontend/.../resourceSearchPolling.ts` | 资源搜索 30s 硬超时伪造 failed 并清空已加载快照;服务端任务其实仍在跑 | 超时上限显著提高或由服务端状态驱动;超时保留旧快照并提供继续等待 |
| P2 | `frontend/.../OrganizationResultPanel.vue` | 整理轮询 120s 静默停止,状态卡"整理进行中" | 超时提示"仍在后台执行"并允许手动续轮询 |
| P2 | `api/library.py` 分页/`library_snapshot.py` | 列表分页全量加载 + N+1,万级文件库内存尖峰 | COUNT/聚合 + join 批量取最新 run + 短时缓存 |
| P2 | `security.py` 限流/STRM 白名单 | 基于 `request.client.host`,反代部署下退化为全局共享桶(可被 5 次失败登录锁全实例) | 可信代理解析 X-Forwarded-For 或按会话维度限流 |
| P2 | `config.py` cookie | `cookie_secure` 默认 False,HTTPS/Tailscale 暴露时会话明文传输 | 默认 True 或启动时对 HTTPS 暴露 + secure=False 告警 |
| P2 | `api/settings.py` 日志导出 | 无上限全量累积内存 | 流式写响应或导出上限 |
| P3 | `mcp.py` / `tasks.py` / `p115_permanent_delete_transport.py` / `tmdb.py` | ✅ 已修复(2.2 #16/#15/#18/#20) |

### 部署与治理建议

1. **发布**:本报告附带的修复在功能分支 `fix/product-hardening-2026-08-10`,经 `scripts/verify.sh`
   与全量测试后合入 `codex/publish-main`,再构建发布包 `watch-assistant-<hash7>-<date>.tar.gz` 部署。
2. **部署实例需重启才能生效**后端修复(busy_timeout、调度重试、解析器、权限校验、webhook/MCP/扫描等);
   前端修复需重新构建 dist(当前部署实例运行的是旧前端)。
3. 建议将 `e2e/live` 套件纳入发布门禁(单独计划运行,不与离线单测混跑)。
4. 待办优先级:下个迭代建议按本表自上而下实施,重点是 115 写路径超时/重试
   与整理"计划-执行"一致性——这是自动整理首轮必现的两类问题。

---

## 6. 变更清单

- 后端(第一轮):`db.py`、`worker.py`、`media_parser.py`、`validation.py`、`subscriptions.py`、
  `library_scan_scheduler.py`、`api/library.py`、`adapters/prowlarr.py`、
  `adapters/qbittorrent.py`、`adapters/p115_playback_gateway.py`
- 后端(第二轮):`services/webhooks.py`、`services/tasks.py`、`services/mcp.py`、
  `services/p115_login_devices.py`、`services/library_scan_operations.py`、
  `adapters/p115_permanent_delete_transport.py`、`adapters/tmdb.py`
- 前端:`App.vue`、`views/LibraryWorkbenchView.vue`(含 busy 死锁修复与 CID 脱敏)、`views/SettingsView.vue`、
  `playwright.config.ts`(testIgnore 增加 `**/live/**`)
- 新增:`frontend/playwright.deploy.config.ts`、`frontend/e2e/live/**`(4 个 spec/setup 文件)
- 测试:第一轮 `test_media_parser.py`(+7)、`test_library_scan_scheduler.py`(+1);
  第二轮 `test_webhooks.py`(+1)、`test_worker_recovery.py`(+1)、`test_mcp_pwa.py`(+1)、
  `test_p115_login_devices.py`(+1)、`test_p115_delete.py`(+1)、`test_library_scan_operations.py`(+1)、
  `test_tmdb_retry.py`(新增文件,+3)
