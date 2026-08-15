# 订阅、季度搜索与自动清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复剧集季度搜索空结果体验，在详情页增加订阅入口，实现订阅智能暂停，并实现受管范围内自动清理垃圾/空文件到回收站。

**Architecture:** 前端在 `MovieView.vue`/`App.vue` 增加订阅按钮与季度空态；后端在 `SubscriptionService.check()` 增加完整性评估并自动暂停；自动清理复用 `small_file_cleanup`、`empty_directory_cleanup_plan`、`media_parser`，通过 `OrganizationAutomationService` 扩展独立自动清理开关。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Vue 3 / TypeScript / Vitest / Playwright

## Global Constraints

- 不永久删除文件；清理只走 115 回收站。
- 不清理未受管目录、扫描不完整或 `uncertain` 的范围。
- 用户可见文案使用中文。
- 所有新增状态变化写审计事件；敏感信息不落日志。
- 每阶段必须通过 `scripts/verify.sh`。
- 真机验证部署到 `192.168.6.236`，`POSTDEPLOY_RELEASE_CHECK=ok`。

---

## Phase A: 剧集季度搜索空态

### Task A1: 前端季度空态文案

**Files:**
- Modify: `frontend/src/views/MovieView.vue`
- Test: `frontend/tests/MovieView.spec.ts`

**Interfaces:**
- Consumes: `props.seasonNumber`, `props.resourceTotal`, `props.resourceLoading`
- Produces: 无新接口，仅在模板中增加空态块。

- [x] **Step 1: 写失败测试**

在 `frontend/tests/MovieView.spec.ts` 增加：

```ts
it("shows season empty hint when a season is selected and total is 0", () => {
  const wrapper = mount(MovieView, {
    props: {
      result: tvResult,
      seasonNumber: 1,
      resourceTotal: 0,
      resourceLoading: false,
      pushingId: null,
      favorite: false,
    },
  });
  expect(wrapper.text()).toContain("当前季度暂无独立资源");
});
```

- [x] **Step 2: 运行测试确认失败**

Run: `npm --prefix frontend test -- --run tests/MovieView.spec.ts`
Expected: FAIL，因为模板没有该文案。

- [x] **Step 3: 实现**

在 `MovieView.vue` 的 `ResourceTable` 前增加：

```vue
<div v-if="selectedSeasonNumber !== null && !resourceLoading && (resourceTotal ?? 0) === 0" class="season-empty">
  <p class="empty-copy">当前季度暂无独立资源</p>
</div>
```

- [x] **Step 4: 运行测试确认通过**

Run: `npm --prefix frontend test -- --run tests/MovieView.spec.ts`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add frontend/src/views/MovieView.vue frontend/tests/MovieView.spec.ts
git commit -m "feat(media): 季度无独立资源时显示明确空态"
```

---

## Phase B: 详情页订阅按钮

### Task B1: MovieView 增加订阅按钮与事件

**Files:**
- Modify: `frontend/src/views/MovieView.vue`
- Test: `frontend/tests/MovieView.spec.ts`

**Interfaces:**
- Produces: `props.subscribed: boolean`、`emit('subscribe')`、`emit('manageSubscriptions')`

- [x] **Step 1: 写失败测试**

```ts
it("renders subscribe button and emits subscribe", async () => {
  const wrapper = mount(MovieView, { props: { ..., subscribed: false } });
  const btn = wrapper.getByRole("button", { name: "订阅" });
  await btn.click();
  expect(wrapper.emitted("subscribe")).toBeTruthy();
});
```

- [x] **Step 2: 运行测试确认失败**

Run: `npm --prefix frontend test -- --run tests/MovieView.spec.ts`
Expected: FAIL

- [x] **Step 3: 实现**

在 `MovieView.vue` script 增加 `subscribed: boolean` prop 和 `subscribe`、`manageSubscriptions` emit；在收藏按钮旁增加：

```vue
<button class="secondary-button" type="button" @click="subscribed ? $emit('manageSubscriptions') : $emit('subscribe')">
  {{ subscribed ? '已订阅' : '订阅' }}
</button>
```

- [x] **Step 4: 运行测试确认通过**

Run: `npm --prefix frontend test -- --run tests/MovieView.spec.ts`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add frontend/src/views/MovieView.vue frontend/tests/MovieView.spec.ts
git commit -m "feat(media): 详情页增加订阅按钮"
```

### Task B2: App.vue 接入订阅状态与创建/跳转

**Files:**
- Modify: `frontend/src/App.vue`
- Test: `frontend/tests/App.spec.ts`

**Interfaces:**
- Consumes: `api.subscriptions()`、`api.createSubscription(payload)`
- Produces: `detailSubscription: SubscriptionResponse | null`、`loadDetailSubscription()`、`handleSubscribe()`

- [x] **Step 1: 写失败测试**

在 `frontend/tests/App.spec.ts` 模拟 `api.subscriptions` 返回一条匹配订阅，断言 `MovieView` 收到 `subscribed=true`。

- [x] **Step 2: 运行测试确认失败**

Expected: FAIL

- [x] **Step 3: 实现**

在 `App.vue`：
- 增加 `const detailSubscription = ref<SubscriptionResponse | null>(null);`
- 在 `openMovie`/`selectSeason`/`syncRoute` 中调用 `await loadDetailSubscription();`
- `loadDetailSubscription()` 拉取 `api.subscriptions()`，匹配 `tmdb_id + media_type + season_number`。
- `handleSubscribe()` 调用 `api.createSubscription({ tmdb_id, media_type, season_number })`，成功后刷新状态并 toast。
- `handleManageSubscriptions()` 导航到 `subscriptions` 视图。

- [x] **Step 4: 运行测试确认通过**

Run: `npm --prefix frontend test -- --run tests/App.spec.ts`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add frontend/src/App.vue frontend/tests/App.spec.ts
git commit -m "feat(media): 详情页订阅状态与创建接入"
```

---

## Phase C: 智能订阅暂停

### Task C1: 新增订阅完整性评估服务

**Files:**
- Create: `src/watch_assistant/services/subscription_completeness.py`
- Test: `tests/unit/test_subscription_completeness.py`

**Interfaces:**
- Produces:
  - `async def evaluate_subscription_completeness(*, session_factory, search_result, season_detail, inventory_files) -> bool`
  - `def resources_cover_all_episodes(resource_names: list[str], season_detail) -> bool`
  - `def inventory_covers_all_episodes(inventory_files: list[InventoryIdentity], season_detail) -> bool`

- [x] **Step 1: 写失败测试**

```python
def test_resources_cover_all_episodes_when_full_season_pack_present():
    season = make_season(episode_count=10)
    names = ["Show S01 1080p COMPLETE"]
    assert resources_cover_all_episodes(names, season) is True
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_subscription_completeness.py -q`
Expected: FAIL

- [x] **Step 3: 实现**

使用 `parse_media_filename` 解析资源名，使用 `build_episode_matrix` 判断 `missing_episodes` 为空且 `conclusion_available` 为真；本地库存同理。

- [x] **Step 4: 运行测试确认通过**

Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/watch_assistant/services/subscription_completeness.py tests/unit/test_subscription_completeness.py
git commit -m "feat(subscription): 新增整季完整性评估服务"
```

### Task C2: SubscriptionService.check 自动暂停

**Files:**
- Modify: `src/watch_assistant/services/subscriptions.py`
- Test: `tests/unit/test_subscriptions.py` / `tests/integration/test_subscriptions_api.py`

**Interfaces:**
- Consumes: `subscription_completeness.evaluate_subscription_completeness`
- Produces: `subscription.auto_paused` 事件

- [x] **Step 1: 写失败测试**

模拟 `search()` 返回覆盖整季的资源，断言 `check()` 后订阅状态为 `paused`。

- [x] **Step 2: 运行测试确认失败**

Expected: FAIL

- [x] **Step 3: 实现**

在 `SubscriptionService.check()` 中，当 `media_type == tv and season_number is not None` 时：
1. 调用 `season_metadata_service.get(...)` 获取集数；失败则跳过自动暂停。
2. 用搜索结果资源名构造 `EpisodeFileReference` 列表，判断搜索资源完整性。
3. 用 `verified_latest_scan` 读取库存快照，筛选 `tmdb_id + season` 的 `InventoryIdentity`，判断本地库完整性。
4. 任一完整 → 用条件 UPDATE 将状态置为 `PAUSED`，写 `subscription.auto_paused` 事件和站内通知。

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_subscriptions.py tests/integration/test_subscriptions_api.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/watch_assistant/services/subscriptions.py tests/unit/test_subscriptions.py tests/integration/test_subscriptions_api.py
git commit -m "feat(subscription): 整季齐全自动暂停订阅"
```

---

## Phase D: 自动清理垃圾/空文件

### Task D1: 设置与 schema 增加自动清理开关

**Files:**
- Modify: `src/watch_assistant/services/settings.py`
- Modify: `src/watch_assistant/schemas.py`
- Modify: `frontend/src/views/SettingsView.vue`
- Modify: `frontend/src/types.ts`
- Test: `tests/unit/test_settings_service.py` / `frontend/tests/SettingsView.spec.ts`

**Interfaces:**
- Produces: 新增设置字段 `auto_cleanup_junk_files: bool`，默认 `False`

- [x] **Step 1: 写失败测试**

在 settings 测试中断言默认值包含 `auto_cleanup_junk_files=False`，保存 true 后回读为 true。

- [x] **Step 2: 运行测试确认失败**

Expected: FAIL

- [x] **Step 3: 实现**

在 `settings.py` 默认值和 `_validate_organization` 布尔列表增加 `auto_cleanup_junk_files`；在 `schemas.py` 的 `OrganizationSettingsResponse/Patch` 增加字段；前端设置“115 整理”区块增加开关。

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_settings_service.py -q` 和 `npm --prefix frontend test -- --run tests/SettingsView.spec.ts`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/watch_assistant/services/settings.py src/watch_assistant/schemas.py frontend/src/views/SettingsView.vue frontend/src/types.ts
git commit -m "feat(cleanup): 增加自动清理广告垃圾文件开关"
```

### Task D2: OrganizationAutomationService 执行广告垃圾清理

**Files:**
- Modify: `src/watch_assistant/services/organization_automation.py`
- Test: `tests/unit/test_organization_automation.py`

**Interfaces:**
- Consumes: `settings.auto_cleanup_junk_files`, `media_parser` 垃圾识别, `small_file_cleanup`, `empty_directory_cleanup_plan`
- Produces: `library.auto_cleanup.applied` 事件

- [x] **Step 1: 写失败测试**

构造完整扫描快照 + 一个广告垃圾文件 + 一个空目录，开启 `auto_cleanup_junk_files` 后调用自动清理方法，断言删除走回收站 transport，且不抛错。

- [x] **Step 2: 运行测试确认失败**

Expected: FAIL

- [x] **Step 3: 实现**

在自动整理/自动清理流程中，若 `auto_cleanup_junk_files` 开启且扫描完整，则：
1. 使用 `media_parser` 识别广告/宣传垃圾文件。
2. 对小文件、空目录复用现有清理 transport。
3. 全部走 `fs_delete` 回收站，不永久删除。
4. 写审计事件。

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_organization_automation.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/watch_assistant/services/organization_automation.py tests/unit/test_organization_automation.py
git commit -m "feat(cleanup): 自动清理广告垃圾文件与空目录到回收站"
```

### Task D3: 真机验证与部署

- [x] **Step 1: 运行完整验证**

Run: `bash scripts/verify.sh`
Expected: PASS

- [ ] **Step 2: 真机 E2E**

Run: `WA_E2E_USER=admin WA_E2E_PASSWORD=admin npx playwright test -c playwright.deploy.config.ts`
Expected: PASS

- [ ] **Step 3: 构建发布并部署**

```bash
bash scripts/build_release.sh "$(git rev-parse HEAD)"
scp -q release-archive/<date>/watch-assistant-<hash>-<date>.tar.gz root@192.168.6.236:/opt/watch-assistant/incoming/
ssh root@192.168.6.236 'tar --warning=no-unknown-keyword -xzf ... -C /opt/watch-assistant/releases/ && ln -sfn ... /opt/watch-assistant/current && printf "WATCH_ASSISTANT_RELEASE=<full-sha>\n" > /var/lib/watch-assistant/release.env && systemctl restart watch-assistant.service'
```

Expected: `POSTDEPLOY_RELEASE_CHECK=ok`

- [x] **Step 4: Commit 文档**

```bash
git add PROGRESS.md docs/superpowers/plans/2026-08-15-subscription-search-cleanup-plan.md
git commit -m "docs: 记录订阅/季度搜索/自动清理实施计划与部署"
```
