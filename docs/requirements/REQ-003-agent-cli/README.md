# REQ-003：Agent CLI 管理入口

| 字段 | 内容 |
|---|---|
| 版本 | V1.0 |
| 状态 | 开发中 |
| 创建日期 | 2026-07-26 |
| 更新日期 | 2026-07-29 |
| 负责人 | 待指定 |
| 优先级 | P1 |
| 依赖 | 版本化管理 API、Agent Token、REQ-004 |
| 管理对象 | REQ-001、REQ-002、现有搜索/推送/设置能力 |

## 1. 一句话定义

提供官方 `watchctl` 命令行工具，让自动化 Agent 在最小权限、两阶段确认和完整审计下查询并管理 Watch Assistant，而不直接接触数据库、115 Cookie 或任意系统命令。

## 2. 背景与问题

Web 界面适合人工操作，但 Agent 通过页面自动化管理系统容易受界面变化影响，也缺少稳定输出、幂等键和权限范围。直接开放数据库、Shell 或原始 115 API 又会绕过产品保护。

需要一个与 Web 共用业务 API 的受控 CLI，把 Agent 的自然语言决策转换为可发现、可预览、可授权、可追踪的产品操作。

## 3. 产品目标

- Agent 可以发现服务版本、实际能力和自身权限。
- Agent 可以查询系统、媒体库、任务、待确认项和审计记录。
- Agent 可以生成整理、洗版和清理计划。
- 具备授权时可以执行已确认计划和安全的异步任务。
- 输出稳定 JSON/JSONL、退出码和错误码，不依赖文案解析。
- 所有写操作具备幂等、并发冲突和 uncertain 保护。
- 所有 Agent 操作可追踪到 Token、请求、计划、任务和结果。

## 4. 非目标

- 不提供任意 SQL、Shell、URL、文件路径或原始 115 API 透传。
- 不允许读取 Cookie、密钥、Web 密码或其他 Agent Token。
- 不允许 CLI 绕过 Web/API 中的业务规则。
- 不允许普通 Agent Token 永久删除 115 文件。
- 不把 CLI 作为独立业务实现；业务规则仍由服务端负责。

## 5. 用户场景

### 5.1 只读巡检

Agent 查询健康状态、能力、队列积压、失败任务和待确认资源，输出 JSON 汇总，不产生任何写操作。

### 5.2 生成整理计划

Agent 为指定目录生成整理计划，分析影响数量、冲突和风险，再把计划摘要提交给用户确认。

### 5.3 授权执行

用户已为 Agent 授予执行 Scope。Agent 提交计划 ID、digest 和 `--confirm`。如果目录已变化，系统拒绝旧计划并要求重新生成。

### 5.4 任务跟踪

Agent 创建 STRM 全量任务后使用 `task wait` 等待。终端超时只结束等待，不取消或重复提交服务端任务。

## 6. 功能范围

| 编号 | 功能 | 优先级 |
|---|---|---|
| CLI-001 | 安装、版本和配置 | Must |
| CLI-002 | Agent Token 与 Scope | Must |
| CLI-003 | 服务能力与 Schema 发现 | Must |
| CLI-004 | 系统、媒体库和任务查询 | Must |
| CLI-005 | 整理计划与待确认管理 | Must |
| CLI-006 | STRM 全量、增量、校验和清理 | Must |
| CLI-007 | 两阶段计划执行 | Must |
| CLI-008 | JSON/JSONL 和稳定退出码 | Must |
| CLI-009 | 幂等、冲突和安全重试 | Must |
| CLI-010 | Agent 审计和 Token 撤销 | Must |
| CLI-011 | doctor 与 Shell 补全 | Should |

## 7. 详细需求

### 7.1 分发和运行环境

首期支持 Windows PowerShell、Linux Shell 和 macOS，命令固定为 `watchctl`。支持 Python 包安装和容器内执行。

```text
pipx install watch-assistant-cli
watchctl configure --server https://watch-assistant.example.ts.net
watchctl doctor
```

具体包名和发布渠道由技术设计冻结。配置文件不得保存 115 Cookie，Agent Token 使用操作系统凭据存储或当前用户只读文件。

### 7.2 认证和权限

CLI 使用独立 Agent Token，不复用 Web 会话和用户脚本 Token。服务端只保存哈希。

| Scope | 能力 |
|---|---|
| `system:read` | 健康、版本和能力 |
| `library:read` | 媒体库、目录和媒体条目 |
| `task:read` | 任务、进度和错误 |
| `organize:plan` | 生成整理、移动和洗版计划 |
| `organize:execute` | 执行已确认整理计划 |
| `review:write` | 处理 TMDB 待确认和人工覆盖 |
| `strm:read` | STRM 状态、清单和校验结果 |
| `strm:write` | 全量、增量和元数据同步 |
| `cleanup:plan` | 生成清理计划 |
| `cleanup:execute` | 执行已确认受管项清理 |
| `settings:read` | 查看非敏感设置 |
| `settings:write` | 修改允许的非敏感设置 |
| `audit:read` | 查看审计记录 |

规则：

- 默认 Token 只读。
- Token 可限制媒体库、有效期、来源 IP或 Tailscale 网络。
- 执行、清理和设置写入必须显式授权。
- Token 可暂停和撤销。
- 永久删除、Cookie 读取、密钥导出和 Token 管理不开放给普通 Agent。
- 权限不足返回缺失 Scope，不泄漏资源是否存在等越权信息。

### 7.3 能力发现

```text
watchctl system status
watchctl system capabilities
watchctl schema commands
watchctl schema export --output json
```

输出包含：

- 服务版本、API 版本、CLI 兼容范围。
- 当前启用的搜索、115、整理、STRM、直链、元数据和清理能力。
- 当前 Token Scope 和媒体库范围。
- 状态、错误码、分类、地区和策略枚举。
- 命令参数和响应 JSON Schema 版本。

版本不兼容时拒绝写操作；只读操作只有在契约兼容时继续。

### 7.4 命令树

```text
watchctl system status
watchctl system capabilities

watchctl library list
watchctl library show <library-id>
watchctl library scan <library-id> [--path-id <directory-id>] [--dry-run]

watchctl media list [--library <id>] [--state <state>]
watchctl media show <media-id>

watchctl organize plan --library <id> [--path-id <directory-id>]
watchctl organize plans [--state <state>]
watchctl organize show <plan-id>
watchctl organize apply <plan-id> --digest <digest> --confirm
watchctl organize retry <job-id>

watchctl review list [--reason <reason>]
watchctl review show <review-id>
watchctl review match <review-id> --tmdb-id <id> --media-type <movie|tv>
watchctl review classify <review-id> --category <category> --region <region>
watchctl review ignore <review-id> --reason <text>

watchctl strm status [--library <id>]
watchctl strm generate --library <id> --full
watchctl strm sync --library <id> [--path-id <directory-id>]
watchctl strm verify --library <id> [--path-id <directory-id>]
watchctl strm cleanup-plan --library <id>
watchctl strm cleanup-apply <plan-id> --digest <digest> --confirm

watchctl task list [--type <type>] [--state <state>]
watchctl task show <task-id>
watchctl task wait <task-id> [--timeout <seconds>]
watchctl task retry <task-id>
watchctl task cancel <task-id>

watchctl audit list [--actor <agent>] [--since <time>]
watchctl audit show <audit-id>

watchctl webhook test <endpoint-id>
watchctl webhook list
watchctl webhook deliveries [--endpoint-id <endpoint-id>] [--limit <n>]
watchctl webhook retry <delivery-id>
```

正式发布后命令遵循语义化兼容策略，删除或重命名必须经过弃用周期。

### 7.5 输出契约

支持：

- 默认人类可读表格或摘要。
- `--output json`：稳定 JSON，不含 ANSI 和进度动画。
- `--output jsonl`：列表或事件流逐行输出。
- `--quiet`：只输出资源 ID、任务 ID或最终必要结果。
- `--fields`：限制字段，减少 Agent 上下文。

统一 JSON：

```json
{
  "ok": true,
  "api_version": "v1",
  "request_id": "req_...",
  "data": {},
  "warnings": [],
  "next_actions": []
}
```

异步响应包含任务 ID、状态、建议轮询时间和下一步。错误包含稳定 `error_code`、中文消息、是否可重试、缺失权限和前置条件。Agent 以结构化字段决策，不解析中文文案。

### 7.6 退出码

| 退出码 | 语义 |
|---|---|
| 0 | 成功或任务已创建 |
| 2 | 参数或本地配置错误 |
| 3 | 未认证或 Token 失效 |
| 4 | 资源不存在 |
| 5 | 版本、计划或并发冲突 |
| 6 | 权限不足或安全策略拒绝 |
| 7 | 服务或上游暂不可用 |
| 8 | 超时或远端结果 uncertain |
| 9 | 部分成功，需检查结构化结果 |

所有 Agent 场景必须支持非交互执行。缺少 `--confirm` 时立即失败，不等待终端输入。`--wait` 超时只停止等待并返回任务 ID，不重放写请求。

### 7.7 两阶段计划

以下操作必须先计划后执行：

- 115 移动和批量重命名。
- 洗版替换和隔离。
- STRM 与空目录清理。
- 批量规则变更应用到历史资源。

计划包含不可变 ID、digest、创建者、过期时间、目录快照版本、影响范围、动作列表、风险、冲突和规则版本。

执行必须同时提交计划 ID、digest 和 `--confirm`。目录版本、规则或计划有效期变化时返回冲突。`--confirm` 只确认当前计划，不是通用授权。

### 7.8 幂等和重试

- 所有写命令支持 `--idempotency-key`。
- CLI 未提供时自动生成并返回。
- 相同 Token、命令、目标和幂等键复用原任务。
- 网络中断先按请求 ID或幂等键查任务，不直接重发。
- CLI 不自动重试可能到达服务端的非幂等写请求。
- `task retry` 只允许服务端判定为安全可重试的任务。
- uncertain 必须先核对远端，不提供绕过选项。

### 7.9 高风险保护

- `--force` 不能绕过权限、计划、隔离、目录范围和 uncertain 保护。
- 超过影响数量阈值的计划需要 Web 人工批准。
- 默认 Agent 无法永久删除、清空隔离区或创建 Token。
- 路径参数优先使用稳定目录 ID，不接受任意绝对远端路径。
- 连续认证失败、策略拒绝或异常大批量计划可以自动暂停 Token。

### 7.10 配置和诊断

`watchctl configure` 保存服务地址、Token 安全引用、默认输出、超时和默认媒体库。

`watchctl doctor` 检查：

- 服务可达性和 HTTPS。
- Token 有效性和 Scope。
- CLI/API 兼容性。
- 115、整理、STRM 和直链能力。
- 本地配置权限。

诊断不显示 Token、Cookie 或完整密钥。

## 8. 管理界面与 API 要求

Web 设置中心提供 Agent 管理：

- 创建具名 Token，选择 Scope、媒体库和有效期。
- Token 原文只显示一次。
- 查看最近使用、来源、CLI 版本和调用数量。
- 暂停、撤销或重新生成。
- 查看 Agent 计划、任务、冲突和策略拒绝。
- 批准或拒绝高风险计划。

CLI 必须使用正式版本化 API，与 Web 共用校验和状态机。不得建立隐藏管理接口。

## 9. 数据与审计要求

保存 Token 哈希、名称、Scope、媒体库范围、有效期、最近使用和撤销状态。每次请求记录 Agent、CLI/API 版本、请求 ID、命令、目标、计划、digest、幂等键、任务和结果。

审计不可由普通 Agent 删除。中文日志和审计展示遵循 REQ-004。

## 10. 非功能要求

- 常规只读命令在不触发远端扫描时 95% 在 2 秒内返回。
- CLI 请求失败不得影响业务服务。
- JSON Schema 和退出码具备兼容性测试。
- Windows、Linux、macOS 均完成端到端验证。
- 日志和输出经过敏感信息扫描。

## 11. 验收标准

- 只读 Token 不能生成或执行写计划。
- plan Token 可以生成计划但不能执行。
- execute Token 仍需有效计划、digest 和确认。
- 目录变化后旧计划被拒绝且无部分写入。
- 相同幂等键只创建一个任务。
- 网络超时返回可查询标识，不自动重写。
- JSON、JSONL、退出码和错误码符合固定契约。
- `watchctl notification list [--unread-only]`、`notification read <id>` 和 `notification read-all` 使用正式通知 API，并保持统一 JSON/JSONL envelope。
- `watchctl webhook test <endpoint-id>` 仅调用正式的单端点测试投递 API，不直接访问第三方接收端。
- Token 撤销后不能提交新操作。
- 每个操作可追踪到 Agent、计划、任务和最终结果。
- CLI 无法获得 Cookie、真实直链、Web 密码或其他 Token。
- 三个桌面系统的核心流程通过测试。

## 12. 分期建议

1. Agent Token、Scope、审计和 API 版本契约。
2. 只读 CLI、Schema 和 doctor。
3. 计划与待确认管理。
4. 异步任务、STRM 和等待命令。
5. 两阶段执行、高风险 Web 审批和完善审计。
6. Shell 补全、规则导入导出和统计。

## 13. 风险与依赖

| 风险 | 缓解措施 |
|---|---|
| Agent 权限过大 | 默认只读、最小 Scope、库范围和有效期 |
| Agent 理解错误 | 计划摘要、digest、快照版本和 Web 审批 |
| CLI/API 漂移 | 版本协商、Schema、写操作不兼容即拒绝 |
| 重试造成重复写 | 幂等键、任务查询、uncertain 核对 |
| Token 泄漏 | 安全存储、短有效期、撤销和来源限制 |

## 14. 待确认项与建议默认值

| 决策项 | 建议默认值 |
|---|---|
| Token 默认权限 | 只读，限制指定媒体库 |
| Token 默认有效期 | 30 天 |
| 高风险批准 | Web 端人工批准 |
| 永久删除 | 不开放给 Agent |
| 默认输出 | 人类可读；Agent 显式使用 JSON |
| 写操作幂等键 | 强制 |
| 整理和清理 | 强制两阶段计划 |
