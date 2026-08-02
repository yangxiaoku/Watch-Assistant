# 项目进度

本文档是项目进度的唯一可信来源。需求状态以
`docs/requirements/README.md` 和 `docs/requirements/BLOCKERS.md` 为准；历史计划文档只保留
设计背景，不用未更新的 checkbox 判断发布状态。

当前 Git 发布基线：唯一发布分支为 `codex/publish-main`；发布前执行 `git fetch origin`，再以
`git rev-parse origin/codex/publish-main` 的实际解析值为准。本记录不固定可能过期的 commit。
本轮只核对仓库文档和 Git 状态，未生成新的 release 包、未执行部署，
也未对 `192.168.6.236:8115` 做服务器只读核对；因此不能把历史更新日志中的 release 包、
健康检查或线上开关直接当作当前生产状态。详细核对记录见
`docs/更新日志-2026-08-02-发布基线核对.md`。生产实际版本和健康状态必须在发布前重新核对。

整理计划和受管夹具写入契约已验收；生产整理写入、永久删除和生产媒体库 STRM 联动仍未开启。

## 2026-08-03 当前基线状态（未发布）

当前文档分支已在本轮 fetch 后通过普通 `git merge origin/codex/publish-main` 更新到最新发布基线。
本轮没有生成 release 包、执行部署或核对生产服务器，因此当前生产版本、运行模式、数据目录和能力开关均未验证。

- [PR #30](https://github.com/yangxiaoku/Watch-Assistant/pull/30) 已合入当前发布基线，包含基础 STRM
  manifest/cleanup、任务租约恢复、扫描完整性门禁和中文工作台调整；对应代码/测试见
  [STRM manifest 单测](tests/unit/test_strm_manifest.py)、[STRM API 集成测试](tests/integration/test_strm_operations_api.py)、
  [工作台单测](frontend/tests/LibraryWorkbenchView.spec.ts) 和 [工作台 E2E](frontend/e2e/organization-workbench.spec.ts)。
  这些是代码和离线/受管夹具证据，不是生产整理、生产 STRM、播放兼容性或部署证据。
- P115 远程可用性证据已由 [PR #31](https://github.com/yangxiaoku/Watch-Assistant/pull/31) 合入当前基线；对应
  [适配器测试](tests/integration/test_p115_adapter.py)、[任务 API 测试](tests/integration/test_tasks_api.py) 和
  [任务状态单测](tests/unit/test_tasks.py) 仍属于 fail-closed 代码/离线证据。本轮未执行 live 或生产部署，
  不能把它们写成生产可用性或发布完成。
- Prowlarr 离线 readiness 已由 [PR #19](https://github.com/yangxiaoku/Watch-Assistant/pull/19) 和
  [PR #29](https://github.com/yangxiaoku/Watch-Assistant/pull/29) 合入，契约见
  [Prowlarr 契约测试](tests/contracts/test_prowlarr_contract.py)；真实来源仍被 Internet Archive
  上游 timeout 阻断，当前没有已验收的可搜索真实来源配置。
- 发布门禁代码已由 [PR #14](https://github.com/yangxiaoku/Watch-Assistant/pull/14)、
  [PR #15](https://github.com/yangxiaoku/Watch-Assistant/pull/15)、[PR #16](https://github.com/yangxiaoku/Watch-Assistant/pull/16)、
  [PR #22](https://github.com/yangxiaoku/Watch-Assistant/pull/22)、[PR #23](https://github.com/yangxiaoku/Watch-Assistant/pull/23)
  和最新 [PR #35](https://github.com/yangxiaoku/Watch-Assistant/pull/35) 合入；当前基线对应
  [manifest 测试](tests/unit/test_release_manifest.py)、[部署脚本测试](tests/unit/test_release_deployment_scripts.py)、
  [systemd 单元测试](tests/unit/test_systemd_units.py) 和 [发布物 smoke 测试](tests/integration/test_release_archive_smoke.py)
  只证明发布门禁覆盖，未证明已打包或已部署。

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
  改名后的增量对账、恢复和清理验收均已有证据；这些夹具结果不等于生产能力已开启，永久删除保持关闭。
- 发现与搜索：TMDB 榜单、季度资料、PanSou 聚合、BTIH 校验、质量筛选、缓存和多来源/Prowlarr
  离线只读 readiness 已合入当前基线；Prowlarr 真实来源仍受 Internet Archive 上游 timeout 阻断，
  当前不能写成已配置可搜索来源，真实来源对照和生产验收仍按 REQ-023 门禁处理。
- 任务与安全：SQLite 状态机、`uncertain` 防重复提交、Web 会话、CSRF、Bearer Token、加密、
  workflow 阶段关联和中文错误映射已合入当前基线。
- 前端工作台：PR #30 和后续 [PR #32](https://github.com/yangxiaoku/Watch-Assistant/pull/32) 均已合入当前基线；
  后续证据见 [工作台 E2E](frontend/e2e/workbench-360.spec.ts)、[工作流导航测试](frontend/tests/coreWorkflowNavigation.spec.ts)
  和 [前端 API 测试](frontend/tests/api.spec.ts)。这些是代码和离线/浏览器证据，不等于生产部署或完整工作台验收。
- STRM manifest 与空目录清理加固已由 [PR #33](https://github.com/yangxiaoku/Watch-Assistant/pull/33) 合入当前基线；
  对应 [manifest 单测](tests/unit/test_strm_manifest.py)、[空目录计划单测](tests/unit/test_empty_directory_cleanup_plan.py)
  和 [空目录清理契约测试](tests/contracts/test_empty_directory_cleanup_contract.py) 只证明离线 readiness，
  不证明生产 STRM、生产清理或永久删除已开启。
- 发布治理：release manifest 完整性/来源校验、systemd release 权限和回退边界、独立 p115 runtime
  已合入当前基线；这些变更仍需按发布流程从提交后的干净工作树打包并做部署核对。

## 待验收

- `REQ-001`、`REQ-002`：受管夹具和离线阶段证据已具备；生产范围、生产媒体库首次完整扫描、稳定
  播放入口、媒体服务器兼容性、元数据联动和生产清理仍未完成。
- `REQ-004`、`REQ-012`、`REQ-019`：结构化日志、站内通知和 Webhook 阶段能力已具备；完整业务
  事件矩阵、真实外部接收端、渠道健康、重试/死信和重放/重启验收仍待完成。
- `REQ-005`、`REQ-006`、`REQ-023`：搜索召回、qB 检测和来源聚合阶段能力已有离线/受控证据；
  脱敏 PanSou 对照、真实样本基线、第二真实来源和生产来源验收仍阻断；Prowlarr live 验证当前受
  Internet Archive 上游 timeout 阻断。
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
- `REQ-010`：库存派生层和部分生产递归扫描证据已具备；生产媒体库配置、应用内完整新鲜扫描、
  目录增量事件以及整理/隔离/恢复统一账本仍在开发。
- `REQ-020`、`REQ-021`：MCP/PWA 的本地安全基础已具备；远程 HTTPS、断线/重启恢复、真实 Push
  和跨平台兼容性仍在开发与验收准备中。

## 待评审

- `REQ-022`：影视合集与系列管理尚未启动。
- `REQ-030`：多维内容分类与筛选导航仍待冻结分类、状态和库存边界后评审。
- `REQ-013` 编号保留，暂不纳入，不创建需求目录。

## 未合入发布基线的候选

以下远端候选或本地分支均不属于 `codex/publish-main`，不得描述为已发布：

- [PR #34](https://github.com/yangxiaoku/Watch-Assistant/pull/34)：库存扫描和组织门禁后续加固；本次核对时仍为 Open，尚未合入。

本次文档修正使用独立分支提交；其他未合入候选和历史 release 记录必须分别核对，不能以分支名、截图或
旧发布包代替当前基线证据。远端 `origin/codex/publish-20260729` 的旧发布说明仅作历史记录，不是当前发布版本。

## 外部依赖与风险

- P115 `errno=990009`：live runner 使用 3 秒重试；live 不进入离线门禁。
- p115 远程可用性 PR #31 已合入当前基线，但本轮未执行 live；在 live、生产部署和回归证据完成前，相关能力保持未验收。
- Prowlarr 离线 readiness 已具备，但真实来源仍受 Internet Archive 上游 timeout 阻断，不能据此认定可搜索来源已配置。
- iPad Cookie：只允许从 `C:\Users\98275\.115ts-secrets\.p115-cookie` 读取，过期会阻断 live。
- `192.168.6.236`：当前部署和 live 验证的内网单点；本轮未核对实际运行模式、版本、数据目录或
  反向代理暴露方式。
- 任何真实 P115 写操作仍需显式 gate、确认、receipt-before-verify 和回滚证据。
- 离线门禁按 `scripts/verify.sh` 分 unit、integration、contracts 三批执行，避免历史
  `release-archive` 参与测试收集。
