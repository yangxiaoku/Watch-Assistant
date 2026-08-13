# 115 网盘自动签到 — 设计文档

日期:2026-08-13
状态:已批准

## 概述

Watch-Assistant 新增 115 网盘每日自动签到能力。每天按配置时间(默认 00:05)自动调用
115 的积分签到接口,并在成功后把结果(积分、连续天数)记录并通知用户;失败同样通知。
默认关闭(`P115_CHECK_IN_ENABLED=false`),由用户在设置中手动开启,遵循项目 fail-closed 惯例。

## 可行性结论(已实测)

- 签到信息(只读):`GET https://proapi.115.com/{app}/2.0/user/points_sign`
  → p115client `P115Client.user_points_sign(app='android')`,返回
  `{"state": true, "data": {"is_sign_today": ..., "continuous_day": ...}}`。
- 每日签到(写):`POST https://proapi.115.com/{app}/2.0/user/points_sign`
  → p115client `P115Client.user_points_sign_post(app='android')`,返回
  `{"state": true, "data": {"points_num": "...", "continuous_day": ...}}`。
- 实测当前 web cookie(UID/CID/KID/SEID)对 android 签到接口可用:签到信息返回
  `is_sign_today: 1`(当天已签)、连续 7 天;签到 POST 返回 `state: true, points_num: '7'`。
- 旧端点 `webapi.115.com/user/add_hd` 与 `user/checkin` 返回"服务器开小差了",
  不可用;签到已迁移到 `proapi.115.com` 的积分系统。

## 架构

新增三个模块,全部镜像现有模式(library/subscription scheduler、settings、event catalog):

```
src/watch_assistant/services/p115_checkin.py     # P115CheckInService
src/watch_assistant/services/p115_checkin_scheduler.py  # 每日调度器
```

接线在 `app.py` lifespan:当 `p115_check_in_enabled` 且 p115 就绪时启动调度器;
`P115_CHECK_IN_ENABLED=false` 时不起调度器、不触发任何网络调用。

## 组件

### 1. P115CheckInService

构造:`(session_factory, cookie_provider, *, event_logger=None, p115_client_factory=None)`
(工厂用于测试注入 fake)。

- `async def check_in() -> CheckInResult`:
  - 用 cookie_provider 读取 cookie,创建 p115client(app='android')。
  - 调 `user_points_sign_post`;`state != true` 或异常 → 抛 `CheckInUnavailable`。
  - 返回 `CheckInResult(points=str, continuous_day=int, already_signed=bool)`。
  - `already_signed`:若接口返回已签到(今日 `is_sign_today=1` 前置检查或响应语义),
    按成功记录但不重复奖励。
- `async def status() -> CheckInStatus`(只读):调 `user_points_sign`,返回
  `is_sign_today`、`continuous_day`、`is_act_user` 等,供设置页展示。
- 幂等:同一天内调度器只触发一次;失败不自动重试(次日自然再签)。

### 2. P115CheckInScheduler

镜像 `subscription_scheduler`/`library_scan_scheduler` 模式:

- 构造:`(session_factory, service, *, settings_service, event_logger)`。
- `run_forever(stop_event)`:每小时检查一次是否到达配置签到时刻(默认 00:05,
  时区用 `cache_warm_timezone` 一致的本地时区);若已到且今天未签,执行一次签到。
- 日期幂等:调度器维护 `last_run_date`(按配置时区的 `YYYY-MM-DD`,持久化到
  `application_settings` 的 `p115_checkin_last_date` 键),`run_forever` 每次检查
  时若 `last_run_date == 今天` 则跳过,避免同日重复签到;跨天后 `last_run_date`
  更新为今天并执行一次。
- `run_once() -> bool` 供测试直接调用。
- 启动条件由 app.py 保证:`P115_CHECK_IN_ENABLED=true` 且 p115 就绪。

### 3. 配置(config.py)

- `p115_check_in_enabled: bool`(env `P115_CHECK_IN_ENABLED`,默认 `False`)。
- `p115_check_in_time: str`(env `P115_CHECK_IN_TIME`,默认 `"00:05"`,
  格式 `HH:MM`,校验合法)。

### 4. 事件与通知(event_catalog.py + 现有通知管线)

注册两个业务事件:

- `p115.checkin.succeeded`:`LogCategory.BUSINESS`,字段 `points`、`continuous_day`。
  通过现有通知/webhook 管线推送(成功也通知)。
- `p115.checkin.failed`:`LogCategory.BUSINESS`,字段 `error_code`。
  通过现有通知/webhook 管线推送(失败通知)。

### 5. 设置页 UI(SettingsView.vue)

- 开关"115 自动签到"(默认关),保存到 `p115_check_in_enabled`。
- 显示最近签到结果:连续天数、上次签到时间、是否已签今日(来自 `status()` 只读接口)。

## 数据流

1. 调度器在配置时刻触发 `run_once`。
2. 检查 `p115_check_in_enabled` + p115 就绪;未就绪则跳过并记 `p115.checkin.skipped`。
3. 调 `service.check_in()`。
4. 成功 → `p115.checkin.succeeded` 事件(通知)+ 更新设置页状态 + 记录"今日已签"。
5. 失败 → `p115.checkin.failed` 事件(通知)+ 记录错误;不重试。

## 错误处理

| 场景 | 处理 |
|---|---|
| 今天已签 | 视为成功(记录已签),不重复奖励,不通知"重复签到" |
| 115 接口 state=false | 抛 `CheckInUnavailable` → `p115.checkin.failed` 事件 + 通知;不重试 |
| 网络/超时异常 | 同上;次日调度自然再签 |
| cookie 失效/缺失 | `CheckInUnavailable("credential_unavailable")` → 事件 + 通知 |
| 开关关闭 | 调度器不启动,零网络调用 |
| 签到时间非法配置 | config 校验失败,启动报错(fail-closed) |

## 测试(TDD)

- **服务**:mock p115client,fake cookie provider,测
  - 签到成功(返回 points/continuous_day);
  - 今天已签幂等;
  - state=false / 异常 → `CheckInUnavailable`;
  - cookie 缺失 → `credential_unavailable`。
- **调度器**:fake service,测到点触发、未到点不触发、同日不重复、开关关闭不启动。
- **配置**:`P115_CHECK_IN_ENABLED`/`P115_CHECK_IN_TIME` 校验(非法时间拒绝)。
- **事件**:成功/失败事件发射 + 通知字段。
- **契约**:新增事件码在 event_catalog 注册(contract test 覆盖)。
- **集成**:`initialize_database` 后 scheduler 在 app 启动接线(参照现有 scheduler 测试)。

## 验收标准

- `P115_CHECK_IN_ENABLED=true` 且到配置时间 → 自动签到成功,设置页显示连续天数/积分,
  通知收到结果。
- 失败 → 通知收到失败,次日自动重签。
- 开关关闭 → 无任何签到网络调用。
- verify 全绿(unit/integration/contract/frontend),前端 266+ 测试通过。
