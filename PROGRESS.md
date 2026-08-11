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

## 待验收

- `REQ-001`、`REQ-002`：服务器部署的 `249aa582` 版本有受限生产只读快照，为 54 页/12 个目录/43 个文件/54 个唯一对象，整理预览为 1 个
  `needs_review` 且写入 0；受管视频夹具历史记录曾有 40/40，但生产 STRM full 失败（`generated=0`、`failed=40`、verify `valid=0`）。真实整理写入、
  生产 STRM 受管清单、稳定播放入口、媒体服务器兼容性、元数据联动和 STRM/空目录清理仍未完成。
- `REQ-004`、`REQ-012`、`REQ-019`：结构化日志、站内通知和 Webhook 阶段能力已具备；完整业务
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
