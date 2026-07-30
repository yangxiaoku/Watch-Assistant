# REQ-001 / REQ-002 实施与验收证据矩阵

更新时间：2026-07-30  
审计基线：`codex/publish-main@187348b`

本矩阵区分四种状态：

- `已证明`：有对应代码、自动化测试和匹配范围的真实证据。
- `部分证明`：已有实现或离线证据，但仍缺少需求要求的接线或真实验收。
- `未证明`：存在代码草稿、合同或 fake，但不能据此开启能力。
- `阻断`：当前安全门禁必须保持关闭。

## REQ-001 115 影视库自动整理

| Must 项 | 当前状态 | 证据 | 尚缺内容 |
|---|---|---|---|
| ORG-001 目录配置 | 部分证明 | 设置 API/UI、`MediaLibrary`、scope 校验 | 多源 CID 到媒体库的持久映射；待确认/隔离目录模型；目标根与页面配置一致 |
| ORG-002 扫描与变化检测 | 部分证明 | `LibraryIndexService`、断点和完整性门禁、扫描 API | 定时任务尚未按 `source_directory_ids` 自动扫描；当前调度器只唤醒已有操作 |
| ORG-003 视频及伴随文件发现 | 部分证明 | 视频扩展名过滤、解析器和伴随文件合同 | 云端目录中的伴随文件分组尚未接入自动计划；样片/预告片的业务流程未闭环 |
| ORG-004 文件名和技术标签解析 | 部分证明 | `media_parser` 单元/合同测试 | `ffprobe` 远端媒体信息补全和失败审计尚未接入执行链 |
| ORG-005 TMDB 匹配 | 部分证明 | `media_matcher` 与 fake/离线测试 | 定时扫描到匹配、限流、AI fallback 的生产闭环及脱敏真实样本 |
| ORG-006 低置信人工确认 | 部分证明 | 计划状态、确认 API、review 状态 | 待确认工作台与扫描任务的自动入队尚未接通 |
| ORG-007 五大分类 | 部分证明 | 分类规则单元测试 | 目标目录 CID 映射和真实 115 分类目录创建尚未证明 |
| ORG-008 地区归类 | 部分证明 | 地区规则单元测试、地区开关 | 真实目标父目录映射、中文目录规则和多源目录验证 |
| ORG-009 标准命名 | 部分证明 | 命名规则、冲突和安全路径测试 | 真实 115 目标目录/文件名映射；当前目标路径不能自动解析为外部目标 CID |
| ORG-010 冲突与计划预览 | 部分证明 | 不可变 plan hash、revision、过期和前置条件 | 定时扫描生成计划；目标冲突和计划摘要的真实 fixture 证据 |
| ORG-011 移动/重命名/伴随联动 | 部分证明 | C03 live fixture；`LiveP115OrganizationTransport`；receipt 后核对 | 业务整理计划的真实执行；伴随文件联动；多对象失败恢复 |
| ORG-012 洗版/隔离/恢复 | 未证明 | 离线 isolation contract、删除默认关闭 | 受控洗版比较、隔离计划、恢复和 STRM 联动的真实 fixture 证据 |
| ORG-013 完成事件 | 部分证明 | `DirectoryDirtyEvent`/outbox 持久化 | outbox 消费者、目录 dirty 队列和 STRM 增量消费未接通 |

## REQ-002 115 STRM 同步

| Must 项 | 当前状态 | 证据 | 尚缺内容 |
|---|---|---|---|
| STRM-001/002 映射与受管清单 | 部分证明 | `StrmManifestEntry`、唯一 current 约束 | 真实媒体库接线和 pickcode 生命周期字段尚未验收 |
| STRM-003 全量/断点续跑 | 部分证明 | 完整扫描门禁、manifest service 离线测试 | 取消后续跑和 10k fixture 证据 |
| STRM-004 原子生成 | 部分证明 | 根目录 allowlist、临时文件、fsync/replace 测试 | 发布环境输出目录和实际运行验收 |
| STRM-005 动态直链播放 | 未证明/阻断 | playback contract fake、opaque manifest ID | 真实 `download_url`、HEAD/GET/Range、Cookie 失效和媒体服务器兼容性 |
| STRM-006 目录级增量 | 未证明/阻断 | 文件级 `incremental()` 代码 | dirty/outbox 目录级队列、running->dirty、重启恢复 |
| STRM-007 元数据增量 | 未证明 | 当前无完整 metadata worker/outbox 闭环 | 115 元数据下载、未变化零下载、失败独立重试 |
| STRM-008 失效/空目录清理 | 部分证明 | 只处理 manifest、完整扫描前置条件 | 非受管文件保护、清理预览/确认和真实精确清理证据 |
| STRM-009 去重/dirty/重试 | 未证明/阻断 | 数据迁移有部分 dirty 表 | 唯一队列、generation、租约、退避和消费 worker |
| STRM-010 校验/修复工作台 | 未证明 | 基础列表 API | 校验、修复、失败分页和危险操作二次确认 |

## 已取得的真实低风险证据

- C02 只读小目录验收曾以 `success/complete=true` 完成，范围仅覆盖受管小目录的有限分页和已观察对象详情。
- C03 专用 fixture 验收已完成：10 次受控写操作逐次核对，临时根精确回收；组织 transport 的单文件移动/重命名循环也已恢复原状态。
- 本次对当前测试 CID 的只读复验返回 `partial/detail_candidate_missing`，说明该目录当前为空或未返回可详情化对象；该结果不能替代成功证据。

以上证据不授权生产影视库自动整理、STRM 播放、STRM 清理或永久删除。

## 2026-07-30 线上安全收敛

审计发现发布环境的高风险开关与证据矩阵不一致，已在保留配置备份后修正
`/etc/watch-assistant.env` 并重启服务。当前健康接口确认：

- `organization_write_enabled=false`，因此真实移动/重命名 worker 不会启动。
- `permanent_delete_enabled=false` 且 `permanent_delete_contract_verified=false`。
- `strm_cleanup_enabled=false`、`strm_playback_enabled=false`、`strm_playback_contract_verified=false`。
- `organization_plan_enabled=true`、`organization_execution_enabled=true`，但没有写开关就不会领取真实写任务。
- `strm_full` 和 `strm_incremental` 保持启用，仅允许完整快照约束下生成/对账本地受管 manifest；不允许清理或播放。

这次配置修正不是功能验收证据；后续每次发布仍必须比较运行时开关、需求证据和回滚记录。

## 下一阶段硬门禁

1. 为每个配置的源 CID 建立并验证 `MediaLibrary` scope；定时器只能扫描已验证 scope。
2. 将定时扫描、完整快照、计划生成、人工确认/高置信自动策略和审计事件串成一个持久任务。
3. 冻结外部目标目录的创建/解析契约，计划中必须保存目标 CID，不能只保存相对路径。
4. 在专用 fixture 上完成目标目录、伴随文件、冲突、隔离/恢复的真实验收；任意 `uncertain` 停止后续写入。
5. 为 STRM 先完成真实直链只读和媒体服务器矩阵，再开启增量与清理；永久删除保持关闭。

在上述门禁完成前，发布说明只能称为“设置和受控计划/执行基础已上线”，不能称为 REQ-001 或 REQ-002 已完成。
