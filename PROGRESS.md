# 项目进度

本文档是唯一可信的进度来源。历史计划文档保留作设计背景，不再用未更新的 checkbox 判断
发布状态。提交号以 `codex/publish-main` 为发布基线。

当前生产发布基线：`codex/publish-main` @ `cdd46c4`（2026-07-30）；release
`watch-assistant-0bb0815-20260730-0655.tar.gz` 已部署到 `192.168.6.236:8115`，健康检查通过。
整理计划和受管夹具写入契约已验收；生产整理写入、永久删除和生产媒体库 STRM 联动仍未开启。

## 已发布

- 115 真实操作门禁：整理计划/执行、移动/重命名写入、永久删除和 STRM 播放均受独立
  开关与契约门禁控制。固定测试 CID 的组织闭环成功；STRM 视频夹具已通过正式 API 的
  范围验证、完整扫描、全量生成、改名后的增量对账、恢复和清理验收。

- 发现与搜索：TMDB 榜单、季度资料、PanSou 聚合、BTIH 校验、质量筛选和缓存；对应历史
  合并提交已纳入 `codex/publish-main`。
- 任务与安全：SQLite 状态机、uncertain 防重复提交、Web 会话、CSRF、Bearer token 和加密；
  对应 `codex/publish-main` 基线提交。
- 115 只读与组织契约：受限目录网关、分页校验、组织计划/执行离线契约和 playback contract；
  对应 `12f9db9`、`d5258c5`、`a3a66da`、`2ad6dae`、`a71405b`。
- 当前通知/工作流候选：后端订阅、通知、工作流、CLI/MCP/PWA 模块及前端中心；对应
  `ee9917a`、`e6aa158`、`44834fb`、`7ddcfb3`。

## 保留中的待裁决分支

以下分支仍有独立提交或被其他 detached worktree 依赖，均不属于当前发布版本。合入前必须基于发布基线验证并登记结果；不得把它们描述为已发布：

- `codex/integration-library-phase-1-c03-clean`、`codex/integration-library-phase-2-clean`、
  `codex/integration-library-phase-2-current`：C03 runner、fixture 和组织 transport 工作树，
  均有未提交修改。
- `feature/integration-inspection-deploy`、`feature/library-migration-foundation`、
  `feature/watch-assistant`：关联 worktree 有未提交改动或验收资产。
- 未注册的 `D:/115ts/.worktrees/library-workbench-audit` 目录仍有残留资产，暂不删除。

30 条无 worktree 的旧分支已归档为 `archive/20260729/...` 标签并删除，详见
`docs/ops/branch-governance-audit-20260729.md`。远端 `origin/codex/publish-20260729` 保留，
因其仍有独立旧发布说明提交 `05f22e7`，不得误报为当前发布版本。

## 未入库工作树

以下资产位于 worktree 工作区，尚未形成提交，不能视为已发布，也不能直接删除：

- `codex/integration-library-phase-2-current`：387 项变更（65 个已跟踪文件、320 个新文件），
  含库/组织/前端/测试的大批候选实现；需要单独拆分和验证。
- `feature/watch-assistant`：23 项变更，主要是搜索、缓存和维护功能候选。
- `codex/integration-library-phase-2-clean`、`codex/integration-library-phase-1-c03-clean`：
  C03 runner/fixture 变更和验收截图，需先确认是否已被发布基线覆盖。
- `feature/library-migration-foundation`：迁移/fixture 相关未入库文件，需单独复核。
- `feature/integration-inspection-deploy`：仅有 production acceptance 截图，待归档或补充说明。
- `library-workbench-audit` 目录存在未登记的残留修改，需恢复为正式 worktree 后再裁决。

## 待验收

- `REQ-001`：生产影视库的业务整理仍需先走预览、确认和审计；受管 fixture 的移动/重命名闭环已验收。
- `REQ-002`：受管视频夹具的正式 API STRM 全量/增量/清理验收已完成；本轮补齐整理完成
  事件的目录 generation 队列、同目录去重、`running -> dirty -> queued` 重入队、租约
  恢复和旧事件迁移回填（专项 25 项测试通过）。本轮进一步为 STRM API 和 dirty worker
  接入可选 workflow `strm` 阶段关联；后续仍需生产影视库首次扫描、稳定播放入口和用户
  目录级回归。
- `REQ-009`：整理 operation 已支持通过受保护 API 关联 workflow，并在排队、claim、成功、
  失败、不确定、取消和重试时同步 `organization` 阶段；STRM API 和整理完成后的 dirty
  worker 已在开始、成功、失败、等待外部和跳过时同步 `strm` 阶段。可靠通知、运行中子
  任务取消和跨任务关联仍待完成。
- `REQ-003`：完成订阅追更，验收为幂等观察记录和可审计通知。
- `REQ-004`：完成库存去重，验收为扫描不完整时禁止清理并提供预览。
- `REQ-005`：完成字幕与技术信息管理，验收为未知值保留且不泄露原始路径。
- `REQ-006`：完成 Task 12 的 e2e、部署和 README 验收证据。

历史别名：`req003` 对应 `REQ-003`，`req004` 对应 `REQ-004`，`038` 对应原发布候选记录，
`c03`/`c05` 对应 P115 合同，`o01`/`o03` 对应组织操作，`r01`/`r02` 对应媒体替换。

## 外部依赖与风险

- P115 `errno=990009`：live runner 使用 3 秒重试；live 不进入离线门禁。
- iPad Cookie：只允许从 `C:\Users\98275\.115ts-secrets\.p115-cookie` 读取，过期会阻断 live。
- `192.168.6.236`：当前部署和 live 验证的内网单点。
- 任何真实 P115 写操作仍需显式 gate、确认、receipt-before-verify 和回滚证据。
- 离线门禁按 `scripts/verify.sh` 分 unit、integration、contracts 三批执行，避免历史 `release-archive` 参与测试收集。
