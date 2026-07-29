# 项目进度

本文档是唯一可信的进度来源。历史计划文档保留作设计背景，不再用未更新的 checkbox 判断
发布状态。提交号以 `codex/publish-main` 为发布基线。

当前基线：`codex/publish-main` @ `793a9c9`（2026-07-29）。生产服务已部署并通过健康检查；
真实 115 写入、永久删除、STRM 写入/清理/播放开关均保持关闭。

## 已发布

- 115 真实操作门禁：整理计划/执行、移动/重命名写入、永久删除和 STRM 全量/增量/清理/播放均已部署；生产开关保持关闭，真实写入仍需受管 fixture 契约验收。

- 发现与搜索：TMDB 榜单、季度资料、PanSou 聚合、BTIH 校验、质量筛选和缓存；对应基线早期
  `feature/watch-assistant` 到 `codex/publish-main` 的合并提交。
- 任务与安全：SQLite 状态机、uncertain 防重复提交、Web 会话、CSRF、Bearer token 和加密；
  对应 `codex/publish-main` 基线提交。
- 115 只读与组织契约：受限目录网关、分页校验、组织计划/执行离线契约和 playback contract；
  对应 `12f9db9`、`d5258c5`、`a3a66da`、`2ad6dae`、`a71405b`。
- 当前通知/工作流候选：后端订阅、通知、工作流、CLI/MCP/PWA 模块及前端中心；对应
  `ee9917a`、`e6aa158`、`44834fb`、`7ddcfb3`。

## 待裁决分支

以下分支仍有独立提交，均不属于当前发布版本。合入前必须基于发布基线验证并登记结果；不得把它们描述为已发布：

- `codex/c05-p115-playback-contract`、`codex/c05-runner-deadline`：C05 playback 合同和 runner deadline。
- `codex/integration-library-phase-1-c03-clean`、`codex/integration-library-phase-1-ui`、
  `codex/integration-library-phase-2-clean`：C03 只读验证、fixture 和组织界面变体。
- `codex/library-capability-matrix`、`codex/library-directories-ui`：库能力和目录配置界面。
- `codex/library-readonly-index-clean`、`codex/library-review-workbench-clean`、
  `codex/library-workbench-audit`：库索引/审核工作台变体，需去重裁决。
- `codex/o01-organization-operation-core`、`codex/o03-companion-preview`、
  `codex/organization-companion-groups`、`codex/organization-plan-preview-clean`：组织操作扩展。
- `codex/phase1-readonly-gateway`、`codex/phase1-readonly-gateway-squashed`：只读 gateway 变体，需保留一个。
- `codex/r01-r02-replacement`：媒体替换策略。
- `feature/frontend-catalog-navigation-v2`、`feature/integration-library-phase-1`、
  `feature/library-migration-foundation`、`feature/library-readonly-index`、
  `feature/library-review-workbench-ui`：前端导航和库基础设施。
- `feature/media-classification-naming`、`feature/media-parser-core`、`feature/tmdb-match-core`：媒体解析与匹配。
- `feature/organization-plan-core`、`feature/organization-review-api`：组织计划与审核 API。
- `feature/p115-library-contracts`、`feature/p115-library-marker-contract`、
  `feature/p115-library-readonly-probe`、`feature/p115-pickcode-contracts`、
  `feature/p115-playback-contracts`：P115 合同和探针。
- `feature/search-ranking-seasons-warm-v2`：搜索排序与预热。

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

- `REQ-001`：受管 fixture 真实整理/移动/重命名验收；生产开关继续关闭直到 receipt-before-verify、回滚和审计证据齐全。
- `REQ-002`：受管 fixture STRM 增量同步验收，确认不落盘 Cookie、token 或时效直链；动态播放契约仍未验收。
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
