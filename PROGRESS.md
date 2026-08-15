# 项目进度

> **SHA 时效说明（2026-08-11）**：本文下方的 `5df17fb` / `249aa582` 是 **2026-08-05 的历史
> 发布线/部署快照**，仅作当时状态记录；当前发布线一律以
> `git rev-parse origin/codex/publish-main` 动态解析为准，勿按本文硬编码值断言当前版本。

本文档是项目进度的唯一可信来源。需求状态以
`docs/requirements/README.md` 和 `docs/requirements/BLOCKERS.md` 为准；历史计划文档只保留
设计背景，不用未更新的 checkbox 判断发布状态。

当前 Git 发布基线：唯一发布分支为 `codex/publish-main`；发布前执行 `git fetch origin`，再以
`git rev-parse origin/codex/publish-main` 的实际解析值为准。本记录不固定可能过期的 commit。
本审计会话未在本地生成 release 包、未执行部署；2026-08-05 动态解析的当前发布基线为
`5df17fb003ae8ea3a4774592d1fc81cb4aa78ead`，其公开 [Verify CI run](https://github.com/yangxiaoku/Watch-Assistant/actions/runs/30984240700)
和 Systemd Release Package 产物 `watch-assistant-5df17fb-20260805-0719.tar.gz` 均成功。已对
`192.168.6.236` 做 systemd、健康接口、生产只读库存/整理预览和 Prowlarr 只读配置/搜索尝试，但未覆盖生产应用 HTTP 鉴权路由、真实
115 写入或媒体服务器兼容性。公开 Actions 结果只证明当前 ref 的离线门禁和发布包校验通过；本次服务器只读核对确认生产仍运行
`249aa5826db6e06683be6426aa5243a1aa4ed0f5`，与当前发布线不一致，仍不能替代完整生产验收。详细历史核对记录见
`docs/更新日志-2026-08-02-发布基线核对.md`。

整理计划和受管夹具写入契约的历史证据已具备；C03 当前因一次性授权已消费而 blocked。生产整理写入、
STRM/空目录清理、永久删除和生产媒体库联动仍未开启。

## 2026-08-05 当前基线状态（发布门禁按 SHA 复核；REQ-023 受限 live 证据日期为 2026-08-04）

本次执行 `git fetch --all --prune` 后，以 `git rev-parse origin/codex/publish-main` 实际解析的完整 SHA
`5df17fb003ae8ea3a4774592d1fc81cb4aa78ead` 作为本次发布线身份（短标识 `5df17fb`）。服务器仍运行
`249aa5826db6e06683be6426aa5243a1aa4ed0f5`，该 SHA 只作为本次生产部署/历史快照身份保留。
最近合入的 PR #50-#61 仅作为 first-parent 历史记录；后续发布必须重新解析实际 ref，不能引用旧报告中的 commit。
Verify 与 Systemd Release Package 必须按同一 `head_sha` 重新核对；历史 run 链接只作为对应快照的证据，不能代替当前发布门禁。
本次核对的公开 [Verify CI run](https://github.com/yangxiaoku/Watch-Assistant/actions/runs/30984240700) 与当前 ref 的
Systemd Release Package 均显示完成且成功。CI 成功仅证明当前 ref 的离线门禁、Compose/发布包校验和发布脚本 readiness gate 通过；
服务器只读核对显示 systemd 当前 release 为 `249aa5826db6e06683be6426aa5243a1aa4ed0f5`、健康接口 `status=ok`，与当前发布线不一致，
当前 `5df17fb` 尚未部署。健康状态还显示整理写契约未验证且整理执行不受支持。
服务器当前部署版本上的生产 STRM full 终态为 `failed`：`generated=0`、`failed=40`、`skipped=3`；只读 verifier
为 `checked=40`、`valid=0`、`invalid=40`，输出 `.strm` 文件数为 0。受管视频夹具的历史/受限记录曾有 40/40，不能替代生产成功；
媒体服务器真实播放兼容、STRM/空目录清理和整理写入继续关闭。
开关状态、契约状态和需求验收不互相替代。

- 2026-08-05 服务器当前部署的 `249aa582` 版本受限生产只读 acceptance preview 成功：递归扫描完整，54 页、12 个目录（含根目录）、43 个文件、54 个唯一对象身份；
  整理计划预览为 1 个 `needs_review`、远程写入 0。生产数据库只读统计显示 2 个媒体库范围均已验证且启用，222 次扫描完整成功、1 次取消；C03 因一次性授权已消费而 blocked。
- 2026-08-04 的 Prowlarr 证据属于历史/受限 Linux ISO 快照：曾观察到 `2.5.2.5491`、`LinuxTracker`、test HTTP `200` 和受限搜索结果；该结果不代表当前可用。
- 2026-08-05 当前 Prowlarr 仍为 `2.5.2.5491`，`LinuxTracker` 已安装并配置，但搜索超时 HTTP `000`，不能写成可搜索完成或聚合完成。

- #44-#61 已进入近期 first-parent 历史，分别覆盖 P1 审查、Prowlarr/PanSou 只读 readiness、库存/整理范围、STRM 清理/提交门禁、前端工作台验收、发布脚本 readiness gate、readiness/可靠性加固、发布基线动态解析、通知失败矩阵、发布后文档、workflow 证据、任务 worker fencing、p115 整理 readiness、验证证据、STRM 清理 readiness、Prowlarr live readiness 和前端工作台。
  这些提交的代码与离线/受管夹具证据不等于生产整理、生产 STRM、播放兼容性、清理或部署验收。
- Prowlarr 的离线 readiness 由 [PR #19](https://github.com/yangxiaoku/Watch-Assistant/pull/19)、
  [PR #29](https://github.com/yangxiaoku/Watch-Assistant/pull/29) 和 [PR #45](https://github.com/yangxiaoku/Watch-Assistant/pull/45) 加固，契约见
  [Prowlarr 契约测试](tests/contracts/test_prowlarr_contract.py)。2026-08-04 历史/受限证据曾在公开 Linux ISO 范围得到 test HTTP `200`、search `17`、
  PanSou `0`、Prowlarr `17`、canonical `17`；2026-08-05 当前搜索超时 HTTP `000`，不能把该历史证据写成当前可搜索完成。
- p115 远程证据仍限于受管夹具、生产只读或 fail-closed 路径。本轮未执行真实写入、真实媒体库整理、STRM 播放/清理或媒体服务器验收，
  不能把 p115 readiness 写成生产可用性或需求完成。
- [发布 manifest 测试](tests/unit/test_release_manifest.py)、[部署脚本测试](tests/unit/test_release_deployment_scripts.py)、
  [systemd 单元测试](tests/unit/test_systemd_units.py) 和 [发布物 smoke 测试](tests/integration/test_release_archive_smoke.py)
  只证明发布门禁覆盖；公开 CI 的 package 成功也不等于自动部署，本次部署状态仍以服务器只读核对为准。

## 2026-08-02 集成审查记录（未发布）

本次文档复核锁定本地 `codex/integration-20260802` 的 `2c342d9` 作为审查快照。它不是
`codex/publish-main`，也没有生成 release 包或执行部署；因此不能把本节内容当作当前生产版本。
复核期间该集成 ref 已前进到 `3afc8d3`，其间的 `2ddcada` 和 `3afc8d3` 源代码/测试提交
当时未纳入本次文档分支；随后随 PR #30 合入发布基线，本节仍只记录当时的审查窗口，不作为生产版本或
需求完成判断。

- 最近集成提交补齐了可恢复扫描的持久目录范围、完整树扫描的页数/总量门禁和游标 v2 校验，
  并继续在范围、断点或页数不可信时 fail-closed。
- STRM dirty worker 和 API operation 现在共享持久租约边界；过期恢复、并发领取和未确认的整理
  替换结果继续保持互斥或 `uncertain`，不会推断远端删除或重复写入。
- 对应回归文件为 `tests/integration/test_library_scan_scope_recovery.py`、
  `tests/unit/test_library_scan_operations.py`、`tests/unit/test_library_index.py`、
  `tests/unit/test_directory_dirty_worker.py`、`tests/unit/test_strm_operations.py` 和
  `tests/integration/test_strm_operations_api.py`。本次文档分支未新增代码或测试通过计数。
- `3afc8d3` 之后新增的 STRM manifest/cleanup 和 task worker lease 结果随后在 PR #30 中合入；
  它们仍只提高 fail-closed readiness，不关闭生产范围、播放兼容性或部署验收阻断。

## 已合入当前基线的代码与离线证据

- 115 受控能力：整理计划、执行、移动/重命名写入、永久删除和 STRM 播放均受独立开关与契约
  门禁控制。固定测试 CID 的组织闭环，以及受管视频夹具的 STRM 范围验证、完整扫描、全量生成、
  改名后的增量对账、恢复和清理验收均已有历史证据；受管视频夹具的历史/受限记录曾有 STRM 全量生成 40 个并 verify 40/40，
  但 2026-08-05 生产 full 终态为 `failed`（`generated=0`、`failed=40`、verify `valid=0`）。这些结果不等于媒体服务器播放或生产清理已验收，
  整理写入和 STRM/空目录清理继续关闭。
- 发现与搜索：TMDB 榜单、季度资料、PanSou 聚合、BTIH 校验、质量筛选、缓存和多来源/Prowlarr
  离线只读 readiness 已合入当前基线；REQ-023 的 Linux ISO 搜索结果仅属于 2026-08-04 历史/受限证据，
  2026-08-05 当前搜索超时 HTTP `000`，不覆盖电影/电视剧召回率、95% 基线、广泛来源质量、完整来源对照或生产 HTTP 鉴权路由。
- 任务与安全：SQLite 状态机、`uncertain` 防重复提交、Web 会话、CSRF、Bearer Token、加密、
  workflow 阶段关联和中文错误映射已合入当前基线。
- 前端工作台：PR #30 和后续 [PR #32](https://github.com/yangxiaoku/Watch-Assistant/pull/32) 均已合入当前基线；
  后续证据见 [工作台 E2E](frontend/e2e/workbench-360.spec.ts)、[工作流导航测试](frontend/tests/coreWorkflowNavigation.spec.ts)
  和 [前端 API 测试](frontend/tests/api.spec.ts)。这些是代码和离线/浏览器证据，不等于生产部署或完整工作台验收。
- STRM manifest 与空目录清理加固已由 [PR #33](https://github.com/yangxiaoku/Watch-Assistant/pull/33) 合入当前基线；
  对应 [manifest 单测](tests/unit/test_strm_manifest.py)、[空目录计划单测](tests/unit/test_empty_directory_cleanup_plan.py)
  和 [空目录清理契约测试](tests/contracts/test_empty_directory_cleanup_contract.py) 只证明离线 readiness，
  不证明生产 STRM、生产清理或永久删除已开启。
- 库存扫描与组织门禁已由 [PR #34](https://github.com/yangxiaoku/Watch-Assistant/pull/34) 合入当前基线；
  对应 [组织用户流集成测试](tests/integration/test_organization_user_flow.py)、[组织计划单测](tests/unit/test_organization_plan.py)
  和 [库存索引单测](tests/unit/test_library_index.py) 只证明代码和离线证据，不证明生产媒体库已配置或已整理。
- task worker readiness 已由 [PR #36](https://github.com/yangxiaoku/Watch-Assistant/pull/36) 合入，随后 [PR #42](https://github.com/yangxiaoku/Watch-Assistant/pull/42)
  继续加固租约丢失和续租超时边界；对应 [worker 恢复集成测试](tests/integration/test_worker_recovery.py) 和
  [任务状态单测](tests/unit/test_tasks.py) 只证明恢复/租约边界，不证明生产任务或 115 live 已验收。
- 发布治理：release manifest 完整性/来源校验、systemd release 权限和回退边界、独立 p115 runtime
  已合入当前基线，PR #40 还修复了 systemd 发布驱动权限；当前基线的公开 package CI 已成功，服务器当前 release 也已按发布流程只读核对，
  但这仍不替代完整生产验收。

## 2026-08-14 前端反馈收敛（已发布 aae965f）

- 统一 Toast 体系 + InlineAlert + 顶栏/侧栏状态点；移除 error/warning/offline 常驻横幅；
  推送成功不再自动弹抽屉（改 Toast + 操作按钮）；设置/工作台/详情/订阅/整理历史等页面
  横幅与区块错误全部收敛（详见 `docs/更新日志-2026-08-14-前端反馈收敛.md`）。
- 已部署 192.168.6.236:8115（release aae965f），Vitest 291 / e2e 152 / 发布冒烟通过。

## 2026-08-15 E2E 调试与通知渠道优化（已部署 efb4611 / 789bb83）

- 使用用户提供的个人部署账号对 `192.168.6.236:8115` 做了真实浏览器 E2E：
  `playwright.deploy.config.ts` 18 项全部通过，`playwright.live-all.config.ts` 27 项全部通过。
- 修复后已重新部署 `789bb83`，`POSTDEPLOY_RELEASE_CHECK=ok`。
- 修复资源分页竞态：详情页在资源搜索尚未完成时提前请求 `resources`，导致
  `resource_snapshot_not_found` 404，质量/类型/排序/搜索等筛选无法激活。现在搜索未完成时
  只记录目标路由，等 `loadResources` 完成后再按最新路由加载；服务器日志中相关 404 已消失。
- 修复搜索缓存过期后的“假 ready”：`ResourceSearchJob` 仍保留旧的 `snapshot_revision`，
  但 `SearchCache` 已过期/清理时，`start_resource_search` 仍返回 `ready`，前端随后请求
  `resources` 得到 404，表现为“搜索有时候失败/排序不生效”。现在检测到缓存缺失会重新排队
  触发搜索，并在 `get_resource_search_task` 轮询路径同样校验快照有效性。
- 搜索超时预算从 90s 提升到 180s：前端轮询 120s 后会给“后台继续”的提示，后端继续执行，
  避免 Prowlarr 多查询超时叠加时在 90s 就硬失败。
- 资源筛选/排序改为乐观更新：点击质量、类型、排序等筛选时立即同步按钮 active 状态，
  不等待资源搜索完成，避免搜索较慢时看起来“点了没反应”。
- 修正媒体库工作台 E2E 断言：页面实际标题为“媒体库”，原用例中的“媒体库与 STRM”为过期断言。
- 通知渠道新增“测试”发送：后端 `POST /api/v1/notify-channels/{id}/test` + 设置页“测试”按钮，
  支持飞书 Webhook/Bot/CLI 和 ClawBot 真实渠道测试，发送失败记录 `notify.delivery_failed`，
  成功记录 `notify.test_delivered`；错误码/事件已进前后端目录。

## 待验收

- `REQ-001`、`REQ-002`：服务器部署的 `249aa582` 版本有受限生产只读快照，为 54 页/12 个目录/43 个文件/54 个唯一对象，整理预览为 1 个
  `needs_review` 且写入 0；受管视频夹具历史记录曾有 40/40，但生产 STRM full 失败（`generated=0`、`failed=40`、verify `valid=0`）。真实整理写入、
  生产 STRM 受管清单、稳定播放入口、媒体服务器兼容性、元数据联动和 STRM/空目录清理仍未完成。
- `REQ-004`、`REQ-012`、`REQ-019`：结构化日志、站内通知和 Webhook 阶段能力已具备；通知渠道
  已支持设置页“测试”发送（后端 `POST /api/v1/notify-channels/{id}/test` + 前端按钮）；完整业务
  事件矩阵、真实外部接收端、渠道健康、重试/死信和重放/重启验收仍待完成。
- `REQ-005`、`REQ-006`、`REQ-023`：搜索召回、qB 检测和来源聚合阶段能力已有离线/受限历史证据；
  REQ-023 当前 Prowlarr 搜索超时 HTTP `000`，电影/电视剧召回率、95% 基线、广泛来源质量、完整来源对照和生产 HTTP 鉴权路由仍阻断。
- `REQ-007`、`REQ-008`：订阅调度、观察账本和质量策略阶段能力已具备；自动动作、库存联动、
  确认/拒绝幂等和洗版回滚仍未完成。
- `REQ-009`：workflow、整理/STRM 阶段关联和安全取消已接入；跨任务关联、完整通知矩阵和所有
  子系统的可中止契约仍待完成。
- `REQ-011`、`REQ-014`、`REQ-025`、`REQ-026`、`REQ-029`：规则层或第一阶段实现已具备；库存、
  外部媒体样本、字幕轨道、动漫映射和完整季度联动证据仍待验收。
- `REQ-015`、`REQ-016`、`REQ-017`、`REQ-018`、`REQ-024`：只读导入、存储治理、备份预览、
  媒体库体检和部署诊断阶段已具备；115 分享远程可用性、真实联动、执行、恢复、升级和生产范围验收仍阻断。
- `REQ-027`、`REQ-028`：中文错误反馈和渐进式详情阶段能力已有离线/浏览器证据；剩余动态错误、
  外部集成和完整端到端证据仍待完成。
- `REQ-001`：生产影视库的业务整理仍需先走预览、确认和审计；受管 fixture 的移动/重命名闭环已验收。
- `REQ-002`：受管视频夹具的正式 API STRM 全量/增量/清理验收已完成；本轮补齐整理完成
  事件的目录 generation 队列、同目录去重、`running -> dirty -> queued` 重入队、租约
  恢复和旧事件迁移回填（专项 25 项测试通过）。本轮进一步为 STRM API 和 dirty worker
  接入可选 workflow `strm` 阶段关联；后续仍需生产影视库首次扫描、稳定播放入口和用户
  目录级回归。
- `REQ-009`：整理 operation 已支持通过受保护 API 关联 workflow，并在排队、claim、成功、
  失败、不确定、取消和重试时同步 `organization` 阶段；STRM API 和整理完成后的 dirty
  worker 已在开始、成功、失败、等待外部和跳过时同步 `strm` 阶段；同一目录 coalesced
  generation 现在会把领取前的所有 dirty 事件绑定到同一 lease，并向所有关联 workflow
  扇出 STRM 子阶段状态。可靠通知完整矩阵、独立 STRM 操作子任务和其他跨任务
  关联仍待完成；本轮取消 workflow 时可停止未开始阶段，运行中或
  `uncertain` 子任务保持原状态；推送任务新增 queued 未 claim 的安全取消及 CLI 命令；
  本轮新增运行中整理 operation 的持久取消请求：未开始远端写入时进入 `cancelled`，写入
  已开始时保持 `uncertain`，不伪造远端撤回；
  资源搜索任务现在持久关联 workflow 的 `discovery` 阶段，冲突关联会 fail-closed；
  内容检测批次现在持久关联 workflow 的 `inspection` 阶段，缓存终态、正常完成、部分失败和依赖失败均同步；
  搜索和检测状态提交后会发出带 workflow/correlation ID 的阶段事件，可行动终态进入站内通知；
  推送创建、重试、取消、worker 终态和重启恢复也会发出同一 push 阶段事件；
  整理 operation 审计和 `organization` 阶段事件补齐 operation/workflow/correlation ID；
  独立内容检测失败现在进入站内错误通知，关联 workflow 时复用阶段通知避免重复；
  整理预览进入 `needs_review` 时新增可行动通知，重复预览按计划聚合并可跳转整理工作台；
  本轮已将 workflow 阶段的可行动状态接入站内通知，
  通知继续复用偏好、去重和 Webhook Outbox；本轮修正任务 worker 将 `uncertain` 误报为
  `failed` 的问题，并补齐任务 ID、115 不可用、订阅新资源和备份失败通知。
- `REQ-012/REQ-019`：Webhook 已提供管理员保护的单端点测试通知入口，测试事件只进入该端点
  的持久 Outbox，复用原有签名、重试和死信链路；禁用端点不会创建投递。
  通知偏好已补齐默认 23:00-08:00 静默时段、IANA 时区和错误/安全事件突破静默，后端迁移、
  API、前端控件和离线回归已通过；Webhook 端点列表补充健康状态及投递运营统计（总数、成功数、
  待重试数、死信数、终态失败率和下一次重试时间）；无 workflow 关联的整理操作失败现在生成
  错误级通知并跳转整理工作台，STRM 增量终态失败和订阅搜索失败也会通知设置入口，关联 workflow
  的失败仍只通过阶段通知，避免重复提醒。外部渠道统一运营闭环、真实接收端和完整事件矩阵仍待验收。
- `REQ-003`：已在当前开发分支补充高风险整理计划 Web-only 批准门禁；超过默认 10 个动作时，
  未经 Web workflow 批准不能排队，Bearer Agent/CLI/MCP 不能绕过。该切片尚未合入发布基线；
  生产播放入口、跨平台验收和完整外部写入仍待验收。
- `REQ-004`：完成库存去重，验收为扫描不完整时禁止清理并提供预览。
- `REQ-005`：完成字幕与技术信息管理，验收为未知值保留且不泄露原始路径。
- `REQ-006`：完成 Task 12 的 e2e、部署和 README 验收证据。

## 2026-08-15 订阅、季度搜索与自动清理（开发分支，未合入发布基线）

设计：`docs/superpowers/specs/2026-08-15-subscription-search-cleanup-design.md`；
实施：`docs/superpowers/plans/2026-08-15-subscription-search-cleanup-plan.md`；
分支：`codex/subscription-search-cleanup`（自 `codex/publish-main` 的 `8608f8e` 分出）。

- 季度搜索：选中季度且资源数为 0 时显示「当前季度暂无独立资源」空态（保留刷新入口，不自动回退其他季）。
- 详情页订阅：`MovieView` 新增「订阅/已订阅/已暂停」按钮与 `subscribe`/`manageSubscriptions` 事件，
  `App.vue` 接入订阅状态加载（复用 `GET /api/v1/subscriptions`，按 tmdb_id+media_type+season_number
  匹配）、创建（复用 `POST /api/v1/subscriptions`，`subscription_exists` 幂等提示）与跳转订阅管理；
  含竞态防护（上下文复核）。
- 智能订阅暂停：新增 `subscription_completeness` 服务（搜索资源名/本地库存任一覆盖全部已播出集即判完整，
  整季包语义、特辑排除、TMDB 季集信息不可用 fail-safe）；`SubscriptionService.check()` 对 TV+季度订阅
  评估后自动置 `PAUSED`（可恢复），写 `subscription.auto_paused` 审计事件并生成可跳转订阅管理的站内通知。
- 自动清理：新增设置 `auto_cleanup_junk_files`（默认关闭）；开启后在完整扫描通过后把受管源目录内的
  广告/宣传垃圾文件移入 115 回收站（复用逐候选复核删除，不永久删除），写 `library.auto_cleanup.applied`
  审计事件（小文件/空目录/垃圾文件三计数）并生成站内通知汇总；与现有小文件/空目录清理共用同一安全门禁。
- 验证：`scripts/verify.sh` 通过（unit 1369 / integration / contracts / ruff / 前端 299 / build），
  证据 `evidence/aeff8f4-20260815-161958`；最终 HEAD 复跑通过（`evidence/637b3e6-20260815-165523`）。
  已于 2026-08-16 合入 `codex/publish-main`（HEAD `637b3e6`，已推送 `origin`）并完成生产部署：
  发布包 `watch-assistant-637b3e6-20260815-1723.tar.gz`，部署前备份
  `backup_1d1dd5aa48fb45fbb185b64a1d40484f`，`POSTDEPLOY_RELEASE_CHECK=ok`，
  生产健康检查 release=`637b3e62d6cb9165075489080fefaa31fbd3a604`；
  真机 E2E（Playwright 直连 `192.168.6.236:8115`）18/18 通过（登录门/桌面核心流程/移动端冒烟）。
  另含外部修复：`f3f6247`（partial 缓存稳定 ready）经复核发现空 partial 伪装 ready 的
  Critical，已修复为按快照资源非空判定（`637b3e6` 内含），一并部署上线。

## 2026-08-16 端到端测试与缺陷修复（已上线）

- 新增上线功能 E2E（`frontend/e2e/live/subscription-cleanup.spec.ts`，随 deploy 配置运行）：
  详情页订阅按钮全链路（创建→已订阅→跳转订阅管理→取消清理）、剧集季度切换（无误导错误、
  空态/资源/错误三选一）、自动清理开关持久化并还原；连同既有 18 项共 21/21 通过。
- 发现并修复缺陷：订阅管理页「取消订阅」按钮为死按钮（后端 `/cancel` 端点存在但前端
  `api.ts` 无 `subscriptionCancel`、模板无 `@click`）——补齐方法、`cancel()`、`@click` 与
  单测；修复发布 `2db235e`（verify.sh 通过）已部署，`POSTDEPLOY_RELEASE_CHECK=ok`。
- 测试数据已清理：测试创建的订阅均经 UI 取消（生产仅保留既有用户订阅）。
- 说明：CSRF 防护生效（裸 POST 取消 403，前端带 `X-CSRF-Token` 正常）；115 风控遵守，
  未触发任何真实 115 写入（自动清理开关保持默认关闭）。

## 2026-08-16 第二轮模块交互 E2E 与缺陷修复（已上线）

- 新增第二轮模块交互 E2E（`frontend/e2e/live/module-flows.spec.ts`）：订阅管理完整交互
  （新建→立即检查→暂停/恢复→取消）、详情页收藏往返、目录排序/年份筛选、详情页资源
  来源/排序/每页筛选、日志分类/等级筛选、通知偏好开关往返；连同既有用例共 27/27 通过。
- 发现并修复缺陷：详情页订阅状态未过滤已取消/已完成订阅——取消订阅后详情页仍显示
  「已订阅」，点击只能跳订阅管理、无法重新订阅（后端 `create` 本允许对 `CANCELLED`
  重建）。修复 `loadDetailSubscription` 状态过滤（cancelled/completed 视为未订阅），
  补单测（cancelled→「订阅」、paused→「已暂停」）；修复发布 `2a16487`（verify.sh 通过）
  已部署，`POSTDEPLOY_RELEASE_CHECK=ok`。
- 测试期间排查的疑似问题均为测试自身缺陷（role=tab 可访问名、带计数的按钮名、桌面端
  日志为表格布局、偏好加载竞态），通知偏好保存功能验证正常；测试数据零残留（订阅全部
  取消、收藏/通知偏好已还原）。

## 2026-08-16 第三轮 E2E:深链接/导航/设置分区/移动端(已合入基线)

- 新增第三轮 E2E（`frontend/e2e/live/module-flows2.spec.ts`）8 项：深链接直达剧集详情并
  刷新保持、浏览器后退/前进往返、PWA manifest 与 userscript 入口、设置剩余分区
  （内容安全/资源检测/连接配置/115 自动签到/追更通知）只读浏览、移动端订阅管理/
  详情订阅按钮/设置分区选择器、整理工作台只读视图切换；连同既有用例共 34/34 通过。
- 本轮无产品缺陷（修正测试视口与折叠区展开）；无需部署（仅测试文件与配置变更，
  已推送 `origin/codex/publish-main`，HEAD `a898e56`）。

## 2026-08-16 第四轮 E2E 与 SPA 回退缺陷修复（已上线）

- 新增第四轮 E2E（`frontend/e2e/live/module-flows3.spec.ts`）9 项：无效路由回退、搜索
  空态文案、目录筛选写回 URL 并刷新保持、Prowlarr 配置分区只读、日志保留策略分区、
  详情页检测入口控件、追更通知渠道表单只读、收藏/记录/工作流直达页、移动端直达页
  无横向溢出；连同既有用例共 43/43 通过。
- 发现并修复缺陷：SPA 回退为白名单路由，未知路径（拼写错误/旧书签）直接返回 API
  JSON 404，浏览器渲染原始 JSON。修复：新增 catch-all 路由——未知前端路径回退
  `index.html` 交客户端路由；未知 `/api/*` 保持 JSON 404；缺失静态资源
  （`assets/*.js`）保持 404 不被掩盖；路径穿越防护（候选须落在静态目录内）。
  集成测试覆盖三语义（含根路径 `/` 与既有 `/organization`、`/logs` 白名单回归）；
  修复发布 `e53cfb2`（verify.sh 通过）已部署，`POSTDEPLOY_RELEASE_CHECK=ok`，
  生产实测未知路径 200/根 200/缺失资源 404。

## 2026-08-16 第五轮 E2E:渠道管理/分页/通知筛选/导航(已合入基线)

- 新增第五轮 E2E（`frontend/e2e/live/module-flows4.spec.ts`）9 项：通知渠道创建→删除
  往返（不发送测试消息，凭据脱敏回显验证）、详情页资源分页（下一页可用时翻页并断言
  内容变化）、通知中心四个筛选 tab（全部/未读/需要处理/错误与安全）、任务中心只读
  浏览、返回浏览导航、`/organization-plans` 旧路由直达、日志高级筛选事件码、搜索空
  输入不崩溃、移动端整理工作台直达；连同既有用例共 51/51 通过。本轮无产品缺陷。

## 2026-08-16 第六轮 E2E:剩余功能用户操作全覆盖(已合入基线)

- 新增第六轮 E2E（`frontend/e2e/live/module-flows5.spec.ts`）9 项用户操作：详情页
  「刷新资源」强制刷新、内容检测「开始检测」触发（状态流转不崩溃）、订阅观察面板
  （已发现资源列表）、首页 hero 收藏按钮往返、目录筛选点「全部」清除并恢复 URL、
  资源检测自动检测开关往返持久化、日志自动刷新开关、任务中心阶段筛选、通知中心
  单条未读点击变已读（计数断言）、移动端详情资源筛选与日志页；9 通过 1 容错跳过。
  本轮无产品缺陷（修正 hero 标题提取、检测开关文案、通知已读断言与并发容错）。

## 开发中

- `REQ-003`：CLI 只读和部分受保护命令已完成；高风险 Web 批准、完整任务关联、生产播放入口和
  跨平台端到端验收仍缺失。
- `REQ-010`：生产媒体库配置已只读核对，受限完整快照为 54 页/12 个目录/43 个文件/54 个唯一对象；目录增量事件以及整理/隔离/恢复统一账本仍在开发。
- `REQ-020`、`REQ-021`：MCP/PWA 的本地安全基础已具备；远程 HTTPS、断线/重启恢复、真实 Push
  和跨平台兼容性仍在开发与验收准备中。

## 待评审

- `REQ-022`：影视合集与系列管理尚未启动。
- `REQ-030`：多维内容分类与筛选导航仍待冻结分类、状态和库存边界后评审。
- `REQ-013` 编号保留，暂不纳入，不创建需求目录。

## 未合入发布基线的其他候选

以下远端候选或本地分支均不属于 `codex/publish-main`，不得描述为已发布：
PR #31-#49 已合入本次核对的发布基线，不列入本节；后续分支仍须重新核对远端 ref。

本次后续文档一致性修正使用独立分支提交；其他未合入候选和历史 release 记录必须分别核对，不能以分支名、截图或
旧发布包代替当前基线证据。远端 `origin/codex/publish-20260729` 的旧发布说明仅作历史记录，不是当前发布版本。

## 外部依赖与风险

- P115 `errno=990009`：live runner 使用 3 秒重试；live 不进入离线门禁。
- p115 远程可用性 PR #31 已合入当前基线，但本轮未执行 live 或真实写入；在 live、生产部署和回归证据完成前，相关能力保持未验收。
- 代码与文档 PR #31-#49 均已合入本次核对的发布基线；这些合入记录和 CI 成功不改变生产版本或验收状态。
- Prowlarr 离线 readiness 已具备；2026-08-04 的公开 Linux ISO 搜索属于历史/受限证据，2026-08-05 当前搜索超时 HTTP `000`，因此不等于电影/电视剧召回、95% 基线、广泛来源质量或生产 HTTP 鉴权路由已验收。
- iPad Cookie：只允许从服务器 `/etc/watch-assistant/p115-cookie` 读取，过期会阻断 live。
- `192.168.6.236`：本轮只读核对了 systemd 当前 release、健康接口、生产库存/整理预览和 Prowlarr 受限来源配置/搜索尝试；未核对
  生产应用 HTTP 鉴权路由、反向代理暴露方式，也未执行真实写入。
- 任何真实 P115 写操作仍需显式 gate、确认、receipt-before-verify 和回滚证据。
- 离线门禁按 `scripts/verify.sh` 分 unit、integration、contracts 三批执行，避免历史
  `release-archive` 参与测试收集。
