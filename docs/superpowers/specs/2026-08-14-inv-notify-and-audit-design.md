# 追更通知（飞书）与库存重复检测 —— 设计文档

> 日期：2026-08-14
> 状态：设计待评审（brainstorming 产出，尚未进入实现）

## 背景与目标

用户已用「网易爆米花直挂 115」打通播放链路，Watch Assistant 负责
发现 → 搜资源 → 入 115 → 整理归档。本设计新增两项独立能力，补齐两个
明确痛点：

1. **追更主动通知**：订阅命中新集并成功入 115 后，主动推送到飞书群，
   附跳回 Watch Assistant 剧集页的链接。当前只有站内通知 + 通用 webhook，
   用户不打开页面就不知道新集已入库。
2. **库存重复检测报告（只读）**：盘点整理归档目录，输出「完全重复」与
   「同片多版本」两类报告，本期零写操作，UI 预留置灰的删除/洗版入口。

两条功能相互独立，各自实现、各自验收。

---

## 功能一：追更通知闭环（飞书）

### 范围（第一期）

- **渠道**：仅飞书自定义机器人（群机器人 webhook）。
- **触发**：订阅命中新集并**成功入 115** 后发送。
- **形态**：富文本卡片提醒，附跳回 WA 剧集页的链接。
- **配置**：设置中心页面管理（新增/删除/启停），webhook URL 加密落库、
  UI 不回显明文。
- **跳转地址**：局域网 `http://192.168.6.236:8115`（本期内网可达）。

### 不做（本期明确排除）

- 企业微信 / Telegram 等其他渠道（接口预留扩展，不实现）。
- 在飞书内直接「确认入库 / 忽略」的按钮交互（放后置）。
- 失败 / 断更 / 停播告警。

### 架构

新增通知渠道层，与现有 Notification（站内）和 WebhookEndpoint（通用外发）解耦：

1. **渠道接口** services/notify_channels/base.py
   - Protocol NotifyChannel: 统一 send(payload) 方法。
   - 统一消息载体 NotifyMessage（标题 / 正文 / 跳转链接 / 剧集元数据）。
2. **飞书实现** services/notify_channels/feishu.py
   - POST 飞书自定义机器人 webhook，消息体用飞书富文本/卡片格式。
   - 超时 10s，trust_env=False（与项目其它出网客户端一致，呼应安全审查）。
   - 失败只记录，不影响订阅主流程。
3. **渠道配置与存储** models.NotifyChannel（新表）
   - 字段：id、name、kind（feishu）、webhook_url_encrypted、
     webhook_url_prefix（回显用：URL 首段 + 尾 4 位）、enabled、revision、
     created_at、updated_at。
   - url 用现有 SecretCrypto（Fernet）加密，UI 与 API 响应只回显前缀 + 尾 4 位，
     **绝不回显明文**（呼应凭据存储安全审查）。
4. **通知分发** services/notify_dispatcher.py
   - 订阅命中成功后触发，查询启用的 NotifyChannel，逐个 send，
     投递结果写事件日志。
   - 触发点是订阅服务里「新资源命中 → 推进入库成功」的精确链路，
     而不是笼统的 subscription.scheduler_completed。

### 卡片内容

- 标题：《剧名》 S02E03 已入库
- 正文：命中来源、清晰度/大小（若有）
- 链接：http://192.168.6.236:8115/tv/{tmdb_id}?season=2

### 错误处理与降级

- 飞书投递失败：记录 notify.delivery.failed 事件 + 落一条站内
  Notification 兜底提醒「飞书推送失败」，**绝不影响入库主流程**
  （fail-open on notify，与现有 webhook 一致）。
- 渠道未配置 / 禁用：静默跳过。

### 安全

- webhook URL 加密落库、不回显明文。
- 出网客户端 trust_env=False，不传本地 HTTP_PROXY。
- 跳转链接不含凭据 / Cookie / 完整 token（只含 tmdb_id、season）。

---

## 功能二：库存重复检测报告（只读）

### 范围（第一期）

- **仅检测** organization 整理归档目录（app.state.organization_target_root_id
  对应的目标根）。
- **只读**：不产生任何 115 移动 / 删除 / 重命名。
- 输出两类报告：**完全重复**、**同片多版本（洗版候选）**。
- Web 页面展示，预留置灰的删除 / 洗版入口。

### 不做（本期明确排除）

- 任何真实删除 / 洗版 / 重命名动作。
- 全库（非整理归档目录）范围扫描。

### 判定逻辑（复用，不重造）

- **完全重复**：复用 storage_governance.py 的 exact_duplicate
  语义（同内容指纹 / 同 size），判重键为内容指纹。
- **同片多版本**：复用 media_parser.py（parse_media_filename + NFKC）
  与 normalize.py 的语义识别，按「同一剧集/集数 + 不同清晰度」归组，
  标为「可洗版候选」，不判定为垃圾。

### 数据流

   整理归档目录 -> P115ReadOnlyDirectoryGateway（只读）
     -> LibraryIndexService 扫描快照（复用现有扫描）
     -> inventory_audit（新增只读服务）分组判定
     -> InventoryAuditReport（两类分组 + 汇总）
     -> Web「库存体检」页展示

### 新增组件

- services/inventory_audit.py：只读判定服务，输入扫描快照，输出报告。
- api/inventory.py：GET /api/v1/inventory/audit（触发/读取报告，
  走 library:read scope）。
- Web 页「库存体检」：按剧分组、两类分栏、可筛选排序、每项删除/洗版按钮
  本期 disabled + tooltip「即将上线」。

### 报告字段

- 完全重复：group_id、各副本 object_id / path / name / size、可回收大小。
- 多版本：剧名 / 季集、各版本 name / 清晰度标签 / size、建议保留版本。
- 汇总：重复副本数、可回收总字节（约 X GB）。

### 安全边界（对齐 AGENTS.md 门禁）

- 本期零写、只读网关、只读扫描。
- 预留删除/洗版入口本期不接线；未来接入必须走
  「预览计划 → 人工确认 → 幂等 → 审计」完整门禁，永久删除默认关闭、
  优先回收站。

---

## 测试与验收

- 功能一：渠道加密存储与回显脱敏、飞书消息体格式、投递失败降级、
  订阅命中触发时机（单测）；契约测试锁定飞书 webhook 协议。
- 功能二：重复判定（完全重复 / 多版本 / 边界 case 单测）、只读不产生
  写操作、报告字段与汇总正确、API scope（library:read）。

---

## 风险与依赖

- 飞书自定义机器人 webhook 协议需真实机器人验证（记入 live 测试，不进离线门禁）。
- 局域网跳转地址在手机外网不可达，属已知取舍；后续如需公网可配置化 base_url。
- 功能二依赖 organization 目标根已配置且 scope_verified；未配置时 fail-closed
  提示「整理归档目录未配置」。
