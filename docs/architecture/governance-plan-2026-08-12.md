# Watch-Assistant 架构治理计划（2026-08-12）

## 背景

全项目代码审查（`docs/reviews/2026-08-11-superpowers-code-review.md`）发现项目存在结构性技术债，被维护者概括为"像屎山"。审查报告的 Critical/Important 发现已全部修复并合入 `codex/publish-main`（30 项）。本文档规划**中期架构治理**，目标是降低长期维护成本，不改对外行为、不触发既有安全门禁。

## 现状诊断（基于审查数据）

| 问题 | 量化 | 影响 |
|------|------|------|
| 巨型单文件 | `organization_plan.py` 3.1k 行、`search.py` 2.5k 行、`strm_manifest.py` 1.4k 行 | 单文件多职责，难读难改 |
| 重复状态机模式 | 任务(23)/组织(40)/STRM(11) 各自实现租约+幂等+审计 | 边界条件无法统一保障，曾导致 recover 误杀/提交吞掉等 bug |
| 手工 scope 前缀表 | `security._required_scope`（已 fail-closed 化） | 新增端点漏配会拒绝访问，靠人肉维护 |
| 条件性校验 | 目录范围校验已补强，仍有零星条件路径 | 边角漏风 |
| 死代码 | `recover_after_restart`(tasks)、`redact_mapping`(security)、`capabilityStatusPresentation`(frontend) | 维护成本，误导读者 |

## 治理原则

1. **不重构安全门禁**：所有写操作 fail-closed、租约 fencing、审计不因重构削弱
2. **行为零变更**：每次重构以现有测试全绿为准绳
3. **分阶段小步**：每阶段独立提交、独立验证，不做一次性大爆炸
4. **测试先行**：TDD，重构时先用测试锁定行为

## 阶段规划

### 阶段 A：死代码与冗余清理（低风险，先行）

**目标**：移除已确认无生产调用的代码，降低阅读成本。

候选：
- `tasks.py:recover_after_restart`（模块级，无生产调用者，inspection 有同名方法）
- `security.py:redact_mapping`（无生产调用者，仅自递归 + 测试）
- `frontend/src/statusCatalog.ts:capabilityStatusPresentation`（无引用）

**风险**：低。但移除需同步删测试，且 `redact_mapping` 的脱敏行为未来可能复用——需判断保留价值。

**验收**：`scripts/verify.sh` 通过。

### 阶段 B：统一状态机抽象（中期核心）

**目标**：把任务/组织/STRM 三处的"租约 + 幂等 + 审计"提炼为共享原语。

**现状差异**：
- `tasks.py`：条件 UPDATE + lease_token + fencing，23 处
- `organization_operations.py`：revision CAS + outbox generation + 审计，40 处
- `strm_operations.py`：claim_start NOT EXISTS + lease fence，11 处

**方案**：先提取一个轻量的 `OperationLease` 原语（持有/续期/过期回收/CAS 提交），三个子系统通过适配层接入，不改状态机语义。此为**最长风险最高**的阶段，建议拆成多个小提交逐个子系统接入。

**验收**：每子系统接入后，其专属测试 + 契约测试全绿，且不改变任何对外错误码。

### 阶段 C：scope 声明式化（中期）

**目标**：把 `security._required_scope` 的手工前缀表改为路由装饰器声明。

**现状**：`_required_scope` 已 fail-closed（未知路径拒绝），但仍靠前缀匹配，新增端点需手动补表。

**方案**：引入 `@require_scope("organize:execute")` 装饰器（已有 `require_scope` 依赖），逐步为每个路由显式声明 scope，最终 `_required_scope` 只保留作为兜底断言（校验声明与请求一致）。

**风险**：中。需保证所有路由声明与当前 `_required_scope` 映射一致（已由 security 单测锁定）。

### 阶段 D：巨型文件拆分（长期）

**目标**：拆分 `organization_plan.py`（计划/执行/预览）、`search.py`（搜索编排/缓存/结果归一）、`strm_manifest.py`（生成/恢复/补偿）。

**方案**：按内部模块边界拆分，保持公开 API 不变（服务类名/方法签名不变，仅移动内部实现到子模块）。

**风险**：中。拆分本身不改逻辑，但需防 import 循环。

## 优先级建议

| 阶段 | 优先级 | 理由 |
|------|--------|------|
| A 死代码清理 | 先做 | 低风险、立即降低阅读成本 |
| B 统一状态机 | 核心 | 消掉最多技术债，防止边角 bug |
| C scope 声明式 | 中 | 已有 fail-closed 兜底，非紧急 |
| D 巨型文件拆分 | 后置 | 纯组织性，风险收益比低 |

## 执行纪律

- 每阶段独立分支，合入 `codex/publish-main` 前跑 `scripts/verify.sh`
- 不扩大范围：一个分支只做本阶段一件事
- 每次提交记录在 `docs/reviews/2026-08-11-superpowers-code-review.md` 的修复进展章节

## 已执行（2026-08-12）

- **claim_next 过期 ORGANIZING 转换改原子 CAS**（`organization_operations.py`）：修复并发双 worker 重复发 workflow 事件
- **_record_history 批量查出已存在条目**（`organization_operations.py`）：消除完成事务内 N+1
- **阶段 C scope 治理**（`security.py` + 测试）：
  - 全量枚举 app 所有 `/api/v1` 路由，断言 `_required_scope` 映射完整且 scope 合法（不落入 fail-closed 兜底）——把手工前缀表变成测试锁定契约，未来新增路由漏配立即失败
  - `/libraries/{id}/configuration` 写方法全局映射对齐路由声明 `settings:write`（修复 bearer 需同时满足两 scope 的不一致）
  - require_scope 声明一致性测试覆盖 library/strm/backups
- **`safe_identity` 统一**（`services/identity.py`）：`_safe_identity` 在 4 个模块复制且语义漂移（organization_plan 宽松版 vs companion/isolation 严格版）。统一到严格共享版（isascii+禁空白+禁路径/URL 分隔符），消除安全边界不一致。全量单测通过。

## 评估：`_as_utc` 统一已回退（重复代码 ≠ 语义一致）

扫描发现 `_as_utc` 在 14 个模块重复，尝试统一到共享 `as_utc`，但回退：**不同模块对 None/时区的处理语义不同**（如 `managed_directory_ownership._as_utc(None)` 返回当前时间，而其他模块返回 None），统一成单一实现破坏 6 个测试。

**教训**：重复代码统一前必须先核对语义漂移，不能只看函数名相同。`safe_identity` 之所以成功统一，是因为 4 份定义中 3 份语义一致（严格版），统一收益明确；`_as_utc` 语义差异大，保持各自实现更安全。

## 已执行：STRM 模块重复函数统一

在 `_as_utc` 回退后，遵循"定义完全一致才统一"原则，统一了 STRM 模块中**语义无漂移**的 4 组重复函数到 `services/strm_path.py`：
- `_absolute_path`/`_same_path`（strm_manifest/strm_cleanup_plan/strm_verification，定义一致）
- `_valid_id`（上述 3 模块 + empty_directory_cleanup_plan，定义一致）
- `_has_symlink_component`（strm_cleanup_plan/strm_verification，定义一致）

**未统一**（语义差异大）：`_safe_prefix`（抛不同模块异常）、`_candidates`（字段集/错误码不同）、`_readable_root`（异常类型 + 赋值差异）。这些"相似但不相同"，保持各自实现。

### 补充：`_decode_codes` 统一到 `services/code_utils.py`

`notifications.py` 与 `webhooks.py` 各有一份 `_decode_codes`（解码存储的 JSON 字符串集合），且语义略有漂移：webhooks 版容错空串（`value or "[]"`），notifications 版不容错。统一到共享 `code_utils.decode_string_set`（保留 robust 容错版），两个模块用 `as _decode_codes` 别名引用，调用点不变。测试：`test_notifications` / `test_webhooks` / `test_webhooks_mcp_pwa` 共 14 例全绿。

### 观察：`_canonical_countries`（media_matcher/media_classification）

两份定义存在微小漂移（matcher 版缺 isinstance 检查），未统一。统一收益低且属展示归一逻辑，风险/收益比不合适，保持各自实现。

## 观察：verify.sh 分 shard 偶发 flaky（已定位）

`scripts/verify.sh` 分 shard 并行偶发失败，已定位根因：`test_client_factory_timeout_closes_late_client`（固定 0.4s 轮询窗口在 4 进程负载下不足）。已改为 `wait_for(5s)` + 以结果为准的轮询，连续多次通过。

## 阶段 A 决策：三个死代码候选全部保留（测试即行为规格）

逐一核查后，三个候选**全部是有测试的纯函数**，测试覆盖真实领域语义，非"误导读者"的有害死代码：

| 候选 | 生产引用 | 测试覆盖 | 语义价值 |
|------|---------|---------|---------|
| `tasks.py:recover_after_restart` | 无 | 7 个测试（确认远程状态/完整文件观察/live lease 保留/target 感知） | 恢复语义规格 |
| `security.py:redact_mapping` | 无 | 1 个测试（递归脱敏） | 脱敏行为规格 |
| `frontend:capabilityStatusPresentation` | 无 | 3 断言（label/tone/nextStep） | STRM 能力状态展示规格 |

**决策**：保留全部三个。理由与 `_as_utc`/阶段 B 相同——删除会把行为规格一起删掉，而保留不产生任何运行时成本。真正有害的死代码（**无测试**且误导读者）已在早期 30 项审查批次中清理。后续若恢复功能接线，这些函数可被直接引用。**阶段 A 收敛为"已核查、保留"，从待办移除。**

## 阶段 B 评估结论：统一状态机抽象不落地（语义漂移）

按 `_as_utc` 教训（定义完全一致才统一），实测三个子系统的"租约 + 幂等 + 审计"**在谓词、values、错误契约、重试逻辑上全部不同**：

| 维度 | `tasks.py` | `organization_operations.py` | `strm_operations.py` |
|------|-----------|------------------------------|----------------------|
| 并发原语 | `lease_token` fencing + live-lease 谓词 | revision 整数 CAS（无 token） | `claim_start` NOT EXISTS 子查询 |
| 谓词 | id/state/owner/token/expires_at | id/revision | id/status/`~active_exists` |
| values | 仅 `updated_at` | revision+1 + lease_token=None | status/started_at/heartbeat/lease_* |
| 失败契约 | 返回 `None` | 抛 `OrganizationOperationConflict` | rollback + 返回 `(summary, False)`，含 OperationalError 重试 |

**结论**：唯一的真公共成语是"原子条件 UPDATE + rowcount==1 校验"，但每处仅 ~2 行且错误处理完全不同。提取 `OperationLease` 需把全部差异参数化，抽象比两个具体调用点更难读，且会把 fail-closed fencing 语义参数化掉，风险与 `_as_utc` 统一同源。**阶段 B 终止，保持各自实现**。并发正确性已由既有原子 CAS 测试与契约测试锁定（error code 不变）。

## 阶段 D 试点已执行：拆分 `strm_manifest.py`（1432 → 934 行）

按内部模块边界拆为 4 个文件，公开 API（`__all__`）与既有 import 者完全兼容：

| 文件 | 内容 | 行数 |
|------|------|------|
| `strm_manifest.py` | models + `StrmManifestService` + `_paths`/`_item`/`_rollback_entry`/`_report_progress` | 934 |
| `strm_fs.py` | 纯文件系统写/删/撤销助手 + 词法校验（`_FileMutation`/`_write*`/`_remove*`/`_restore*`/`_read_target`/`_is_managed_content`/`_safe_root`/`_within`/`_valid_relative_path` 等），无 DB 访问 | ~250 |
| `strm_fencing.py` | `_LeaseFence` + 租约 fencing 助手 + `CancelCheck`/`LeaseCheck`/`SessionFence` 类型 | ~230 |
| `strm_errors.py` | `StrmManifestError`（放在最底层避免 fs/fencing 与 main 成环） | 12 |

**要点**：
- 提取用 AST 按源顺序 + 含装饰器，避免丢 `@dataclass` 或乱序
- `StrmManifestError` 移到共享模块后，三处 `is` 同一性断言通过（避免双异常类破坏 `except` 语义）
- 既有 import 者（`strm_cleanup_plan`/`empty_directory_cleanup_plan` 等）从 `strm_manifest` 再导出私有助手，行为不变；后续可选迁移到 `strm_fs`/`strm_fencing` 直接导入
- 88 个 STRM 测试 + `scripts/verify.sh` 全绿

## 阶段 D 第二项：拆分 `search.py`（2461 → 1923 行）

`SearchService` 是 1740 行巨型类（方法间通过 `self` 强耦合，不易拆类）。采用**提取类后模块级纯函数**策略：

| 文件 | 内容 |
|------|------|
| `search_helpers.py` | 33 个纯函数 + `_ResourceSearchTask` + `LEGACY_MAGNET_LIMIT`/`_QUALITY_PATTERNS` 常量：资源归一合并（`_merge_normalized`/`_merge_source_observations`）、质量分类（`_quality_tags`/`_quality_matches`）、快照打分（`_resource_score_snapshot`/`_complete_score_snapshot`）、展示助手（`_resource_summary`/`_resource_facets`/`_filter_media_collection`） |
| `search.py` | `SearchService` + 缓存/锁原语（`make_cache_key`/`_bounded_set*`）+ 异常类 |

**要点**：
- 被 search.py 内定义的名字（`_ResourceSearchTask`/`LEGACY_MAGNET_LIMIT`）随提取移入 helpers，search.py 单向 re-export，无循环依赖
- 既有外部 import（`source_penalty`/`make_cache_key`/`_dedupe_resources` 等 8 个私有名）通过 re-export 保持兼容
- ruff F401/F821 迭代校正 import；113 个 search 测试 + `scripts/verify.sh` 全绿
- `_search_error_code_for_log`/`make_cache_key`/`_bounded_set*` 留在 search.py（服务实例直接依赖）

## 阶段 D 第三项：拆分 `organization_plan.py`（3078 → 1204 行）

最大的文件。`OrganizationPlanService` 类（220-1314）与 ~1760 行模块级助手混在一起。拆为 3 个文件：

| 文件 | 内容 | 行数 |
|------|------|------|
| `organization_plan.py` | `OrganizationPlanService` + `__all__` + 两个子模块 re-export | 1204 |
| `organization_plan_models.py` | 9 个模型类（Status/Error/PlanSource/View/Item/ExecutionStep 等），供 service 与 helpers 共用（避免成环） | 173 |
| `organization_plan_helpers.py` | 51 个模块级助手：payload 构建（`_build_payload`/`_execution_payload`）、校验（`_validate_*`）、可执行步骤（`load_executable_steps`/`_parse_executable_steps`）、快照（`_load_source_snapshot`/`_source_snapshot_changed`） | 1758 |

**要点**：
- 助手块经 AST 验证**不引用 service 内部状态**，可独立提取
- 模型移入共享模块后三处异常类同一性断言通过
- 唯一外部 import 的私有助手 `_execution_blockers`（仅测试用）迁移测试至 `organization_plan_helpers` 直连导入
- `__all__` 11 个公开名字原样保留；153 个 organization 测试 + `scripts/verify.sh` 全绿

**阶段 D 完成**：strm_manifest（1432→934）、search（2461→1948）、organization_plan（3078→1204）三个巨型文件全部拆分。

## 阶段 C 决策：scope 声明式化不落地（安全契约已被测试锁定）

`_required_scope` 手工前缀表仍有 173 路由中 153 个依赖（仅 20 个用 `require_scope` 装饰器）。评估后**不做全量声明式转换**：

1. **fail-closed 已生效**：未映射路径抛 403（`test_unknown_api_path_is_fail_closed_not_default_scope`）
2. **全量枚举已锁定**：`test_all_registered_api_routes_have_an_explicit_scope` 枚举 app 全部 `/api/v1` 路由，断言每个都有显式 scope——新增路由漏配立即失败
3. **一致性已锁定**：`test_require_scope_routes_are_consistent_with_global_mapping` 校验装饰器声明与全局表一致

**决策**：转换为全量 `@require_scope` 装饰器（需改动 security.py + 全部 api 文件）不增加任何安全收益，且安全相关 churn 风险高。与阶段 A/B 同理，**保留手工表 + 测试契约**，从待办移除。**治理计划四阶段（A/B/C/D）全部收敛。**

## 待评估（依赖 transport 能力）

- **`_delete_small_files` 删除前复核 size**（`organization_automation.py`）：live listing 的 `C03RemoteEntry` 不含 size 字段，transport 层未解析 115 响应的文件大小。有效实现需要 (a) 验证 115 响应 size 字段名、(b) 扩展 `C03RemoteEntry` + `_normalize_list_entry`。在当前 transport 能力下无法低成本落地，依赖后续 transport 扩展，暂记录不做。
