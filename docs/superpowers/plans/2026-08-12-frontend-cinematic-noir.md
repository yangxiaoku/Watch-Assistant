# 前端 Cinematic Noir 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以「影院级深色（Cinematic Noir）」美学重设计 Watch Assistant 全部 12 个前端视图，并同步拆解超大文件、组件化、建立设计令牌系统，行为零变更。

**Architecture:** 保留变量名、改变量值的「重新换肤」策略保证低风险渐进。先换令牌（styles.css 的 `:root` 变量值整体替换为 Noir 暖炭黑 + 琥珀金），再重构布局（AppShell/AppSidebar/AppTopbar 侧栏剧院），再提取组合式函数（从 1692 行 App.vue 拆出 8 个 composables），再重设计原语组件，最后逐视图打磨。每步保持 `npm test` 全绿。

**Tech Stack:** Vue 3.5（Composition API）、TypeScript、Vite 8、Vitest + Testing Library + MSW、Playwright e2e、@lucide/vue。零新运行时依赖。

## Global Constraints

- 零新运行时依赖（`dependencies` 仅 `vue` + `@lucide/vue`）。
- `src/api.ts` 一行不动；路由/深链/轮询/离线回退/检查/推送逻辑全部原样保留。
- 只改 `frontend/` 目录，绝不碰后端 `src/`、`tests/`、`docs/`（另一会话在修后端）。
- 所有提交仅在 `codex/frontend-cinematic-noir` 分支（隔离 worktree）。
- 每任务结束：`npm test` 必须全绿（当前基线 29 files / 231 tests）。
- 变量名保留、值替换：现有组件用 `--bg/--surface/--text/--accent/--mint/--gold/--amber/--danger/--sky` 等，只改值不改名，实现全局即时换肤。
- 中文界面文案不改变（`chineseUiCopy.spec.ts` 会校验）。
- 字体全部系统栈，零 CDN。

---

## Phase 1 — 设计令牌（换肤）

### Task 1: 替换 `:root` 令牌为 Noir 调色板

**Files:**
- Modify: `src/styles.css:1-37`

**Interfaces:**
- Produces: 新的令牌值（变量名不变，值与 Type 栈替换）。后续所有任务的视觉基础。

- [ ] **Step 1: 读取 `src/styles.css` 前 40 行，确认变量清单**

- [ ] **Step 2: 替换 `:root` 块**

```css
:root {
  color-scheme: dark;
  --font-display: "Iowan Old Style", "Songti SC", "Noto Serif CJK SC", Georgia, serif;
  --font-body: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;

  --bg: #14120F;
  --bg-soft: #1A1714;
  --surface: #201C18;
  --surface-2: #28231E;
  --surface-3: #2E2A24;
  --border: #2E2923;
  --border-strong: #3D362C;

  --text: #F2ECE1;
  --text-2: #A89F91;
  --text-3: #6C6455;

  --accent: #D8A45E;
  --accent-strong: #E6BD80;
  --accent-deep: #B9853D;
  --accent-soft: rgb(216 164 94 / 12%);
  --accent-glow: rgb(216 164 94 / 24%);

  --mint: #8FB078;
  --gold: #E0B974;
  --amber: #D8A45E;
  --danger: #C96B5B;
  --danger-deep: #A05545;
  --danger-soft: rgb(201 107 91 / 14%);
  --sky: #8BA6A0;
  --sky-soft: rgb(139 166 160 / 14%);

  --radius: 6px;
  --radius-sm: 4px;
  --radius-lg: 12px;
  --shadow: 0 10px 28px rgb(0 0 0 / 35%);
  --shadow-lg: 0 26px 60px rgb(0 0 0 / 50%);
}
```

- [ ] **Step 3: 全文搜索旧色值残留**（`#6c8cff`、`#819dff`、`#4a6cf5`、`#4ade80`、`#f5c76a`、`#f5a64c`、`#f87171`、`#63b3ed`），把硬编码旧色改为对应令牌或 Noir 值

- [ ] **Step 4: `npm test` 全绿**

- [ ] **Step 5: 提交**

```bash
git add src/styles.css
git commit -m "style(frontend): Noir 调色板令牌换肤"
```

### Task 2: 去噪 — 收敛圆角、去渐变按钮/辉光/胶囊

**Files:**
- Modify: `src/styles.css`（组件类）

- [ ] **Step 1: 渐变按钮去渐变**：`.primary-button`、`.filter-options button.active`、`.pagination strong`、`.feature-command`、`.feature-kicker`、`.brand` 改为纯 `--accent` 实底或纯文字色

- [ ] **Step 2: 圆角收敛**：`.brand`/按钮/`.icon-button` 等 999px 胶囊 → 6px；`.rating` 胶囊 → 小标签（保留圆角但非全胶囊）

- [ ] **Step 3: 去辉光**：删除 `box-shadow` 中带颜色辉光的（如 `0 0 26px rgb(... / 16%)`），只保留中性阴影

- [ ] **Step 4: `npm test` 全绿 + `npm run build` 成功**

- [ ] **Step 5: 提交**

```bash
git add src/styles.css
git commit -m "style(frontend): 去渐变/辉光/胶囊噪点"
```

### Task 3: 衬线展示字体接入

**Files:**
- Modify: `src/styles.css`

- [ ] **Step 1: 页面大标题**：`.library-heading h1`、`.organization-heading h1` 加 `font-family: var(--font-display); letter-spacing: .01em;`

- [ ] **Step 2: 数据字体**：日志/大小/时间戳/infohash 相关类（`.logs-*`、`.resource-*` 中数据列）加 `font-family: var(--font-mono); font-variant-numeric: tabular-nums;`

- [ ] **Step 3: 片名衬线**：`.feature-copy strong`、`.movie-card-copy strong` 加 `font-family: var(--font-display)`

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "style(frontend): 衬线展示字体接入"
```

---

## Phase 2 — 布局（侧栏剧院）

### Task 4: 布局组件骨架

**Files:**
- Create: `src/layout/AppShell.vue`
- Create: `src/layout/AppSidebar.vue`
- Create: `src/layout/AppTopbar.vue`
- Create: `src/nav.ts`（导航分组数据模型）

**Interfaces:**
- Produces:
  - `src/nav.ts`: `export interface NavItem { view: BrowseView; label: string; icon: Component }`、`export interface NavGroup { label: string; items: NavItem[] }`、`export const NAV_GROUPS: NavGroup[]`（发现：首页/电影/剧集/搜索；我的：收藏/历史；管理：媒体库/整理/任务中心/通知/日志/设置）。
  - `AppSidebar.vue` props: `{ activeView: BrowseView; organizationPlanEnabled: boolean }`，emit: `navigate(view)`。
  - `AppTopbar.vue` props: `{ authenticated: boolean; query: string }`，emit: `update:query`、`search`。
  - `AppShell.vue` slot 布局，props 透传。

- [ ] **Step 1: 创建 `src/nav.ts`**，三组导航 + 图标映射（用 `@lucide/vue`）

- [ ] **Step 2: 创建 `AppSidebar.vue`**：品牌（`WATCH/ASSISTANT`，衬线）、三组导航（`MarqueeNavItem` 激活态过道灯条）、底部连接状态点

- [ ] **Step 3: 创建 `AppTopbar.vue`**：全局搜索框 + 在线状态点（不再有 5 个快捷按钮）

- [ ] **Step 4: 创建 `AppShell.vue`**：CSS grid 布局（侧栏 240px + 主区），响应式三档

- [ ] **Step 5: 单测 `src/tests/nav.spec.ts`**：断言三组名称、每项 view 合法

- [ ] **Step 6: `npm test` 全绿 + 提交**

```bash
git add src/layout src/nav.ts src/tests/nav.spec.ts
git commit -m "feat(frontend): 侧栏剧院布局骨架"
```

### Task 5: App.vue 接入布局组件，导航收进侧栏

**Files:**
- Modify: `src/App.vue:1592-1612`（模板顶部）
- Test: `tests/App.spec.ts`、`tests/coreWorkflowNavigation.spec.ts`、`tests/chineseUiCopy.spec.ts`

- [ ] **Step 1: 盘点测试对顶栏结构的选择器**（`grep` 测试中的 `topbar`/`primary-nav`/`quick-action`），记录需更新的断言

- [ ] **Step 2: 模板顶部改用 `<AppShell>`**，品牌/导航/搜索移入布局组件；保留离线横幅逻辑

- [ ] **Step 3: `navigateToView` 事件改为布局组件 emit 驱动**，行为一致

- [ ] **Step 4: 更新受影响的测试选择器**（如改查 `data-testid` 或文本）

- [ ] **Step 5: `npm test` 全绿 + `npm run build` + 提交**

```bash
git commit -am "refactor(frontend): App.vue 接入侧栏布局"
```

### Task 6: 响应式三档

**Files:**
- Modify: `src/layout/AppShell.vue`、`src/layout/AppSidebar.vue`

- [ ] **Step 1: >1200px** 完整侧栏（240px）

- [ ] **Step 2: 768–1200px** 侧栏收成图标栏（72px，图标 + tooltip，组标签隐藏）

- [ ] **Step 3: <768px** 抽屉式侧栏（遮罩 + 汉堡按钮）

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "feat(frontend): 侧栏响应式三档"
```

---

## Phase 3 — 组合式函数（App.vue 拆解）

> 原则：每个 composable 从 App.vue 原样搬移对应状态与函数，行为逐字不变；搬完一段提交一段，`npm test` 保持绿。

### Task 7: `useAuth` + `useConnectivity`

**Files:**
- Create: `src/composables/useAuth.ts`
- Create: `src/composables/useConnectivity.ts`
- Test: `tests/composables.spec.ts`

- [ ] **Step 1: 从 App.vue 搬移** login/logout/authenticated/loggingIn/error 相关逻辑到 `useAuth`；`isOnline`/`offlineDataAt`/`browserIsOnline` 到 `useConnectivity`

- [ ] **Step 2: 写单测**：`useAuth.login` 成功/失败路径；`useConnectivity` 初始状态

- [ ] **Step 3: App.vue 改为消费 composable**（模板与其余逻辑不动）

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "refactor(frontend): 提取 useAuth/useConnectivity"
```

### Task 8: `useCatalog`

**Files:**
- Create: `src/composables/useCatalog.ts`
- Test: `tests/composables.spec.ts`

- [ ] **Step 1: 搬移** homeCatalog/catalogMovies/catalogHeading/catalogError/genreId/year/sort/currentPage/totalPages/totalResults 及 `loadHome`/`loadDiscover`/`loadPage`/`retryCatalog`

- [ ] **Step 2: 单测**：分页 clamp、筛选状态迁移

- [ ] **Step 3: App.vue 消费**，删除搬走的 ref 与函数

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "refactor(frontend): 提取 useCatalog"
```

### Task 9: `useSearch`

**Files:**
- Create: `src/composables/useSearch.ts`

- [ ] **Step 1: 搬移** searchInput/query/result/searchSourceNames/searchMovies/result 重置逻辑

- [ ] **Step 2: 单测**：`searchMovies` 触发 API、结果置位

- [ ] **Step 3: App.vue 消费**，删除搬走部分

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "refactor(frontend): 提取 useSearch"
```

### Task 10: `useMediaDetail`（最大一块）

**Files:**
- Create: `src/composables/useMediaDetail.ts`
- Test: `tests/composables.spec.ts`

- [ ] **Step 1: 搬移** 详情状态机：metadata/selectedSeason/seasonDetail/resourceResponse/resource 系列 ref、inspection 系列 ref、push 系列 ref 及对应函数（`loadMetadata`/`selectSeason`/`loadResourcePage`/`changeResourceFilter`/`changeResourceQuery`/`inspectMore`/`retryFailed`/`startPush`/`refreshResources`）

- [ ] **Step 2: 单测**：资源分页/筛选状态迁移、inspection 进度累计

- [ ] **Step 3: App.vue 消费**，删除搬走的 ~600 行

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "refactor(frontend): 提取 useMediaDetail"
```

### Task 11: `useFavoritesHistory` + `useTaskDrawer` + `useCapabilities`

**Files:**
- Create: `src/composables/useFavoritesHistory.ts`
- Create: `src/composables/useTaskDrawer.ts`
- Create: `src/composables/useCapabilities.ts`

- [ ] **Step 1: 三个 composable 搬移**（收藏/历史本地存储；任务抽屉；strm/组织能力开关）

- [ ] **Step 2: 单测**：favorites 增删/持久化、drawer 开关、capabilities 置位

- [ ] **Step 3: App.vue 消费**，删除搬走部分

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "refactor(frontend): 提取收藏/任务抽屉/能力开关 composables"
```

### Task 12: App.vue 清瘦为纯组装层

**Files:**
- Modify: `src/App.vue`（目标 ≤ 300 行）

- [ ] **Step 1: 删除全部已搬移的 ref/函数**，只留布局组装 + 视图分发 + 跨视图事件

- [ ] **Step 2: `npm test` 全绿 + `npm run build`**

- [ ] **Step 3: 代码评审自查**：无重复状态、无死代码

- [ ] **Step 4: 提交**

```bash
git commit -am "refactor(frontend): App.vue 收敛为组装层"
```

---

## Phase 4 — 原语组件

### Task 13: `PosterCard`（替换 `MovieCard`）

**Files:**
- Create: `src/components/PosterCard.vue`
- Delete: `src/components/MovieCard.vue`（或就地改造）
- Test: `tests/MovieView.spec.ts`（改用 PosterCard）

- [ ] **Step 1: 新组件**：海报为主角，hover 琥珀边光，衬线片名，rating 改小字标签（非圆形盘），去 fav 按钮（改为 hover 出现）

- [ ] **Step 2: props 与 MovieCard 对齐**（`movie/favorite/active` 等），保证调用方不改或最小改

- [ ] **Step 3: 更新引用与测试**

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "feat(frontend): PosterCard 海报卡片"
```

### Task 14: `PageHeader` + `SegmentedControl`

**Files:**
- Create: `src/components/PageHeader.vue`
- Create: `src/components/SegmentedControl.vue`
- Test: `tests/PageHeader.spec.ts`、`tests/SegmentedControl.spec.ts`

- [ ] **Step 1: `PageHeader`**：衬线大标题 + eyebrow + 操作区 slot；props `{ eyebrow?: string; title: string }`

- [ ] **Step 2: `SegmentedControl`**：分段控件；props `{ options: { value; label }[]; modelValue }`，emit `update:modelValue`

- [ ] **Step 3: 单测**：标题渲染、选项切换 emit

- [ ] **Step 4: 在 LibraryView / SearchView 试替换** 手写 heading 与胶囊筛选

- [ ] **Step 5: `npm test` 全绿 + 提交**

```bash
git commit -am "feat(frontend): PageHeader 与 SegmentedControl 原语"
```

### Task 15: `ResourceList`（替换 `ResourceTable`）

**Files:**
- Create: `src/components/ResourceList.vue`
- Test: `tests/ResourceTable.spec.ts`（迁移断言）

- [ ] **Step 1: 新组件**：干净列表，数据列等宽字体，徽章降到最少，操作聚焦（推送按钮）

- [ ] **Step 2: props/事件对齐 ResourceTable 调用方**（`MovieView` 里使用处）

- [ ] **Step 3: 更新测试**

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "feat(frontend): ResourceList 资源列表"
```

### Task 16: `EmptyState`/`ErrorState` + `MarqueeNavItem` + `TaskDrawer` 重设计

**Files:**
- Create: `src/components/EmptyState.vue`
- Create: `src/components/ErrorState.vue`
- Create: `src/components/MarqueeNavItem.vue`
- Modify: `src/components/TaskDrawer.vue`
- Test: `tests/EmptyState.spec.ts`

- [ ] **Step 1: 三个新组件**：Noir 语感空态（衬线引导 + 建议）、错误态（发生了什么+怎么修）、侧栏过道灯导航项

- [ ] **Step 2: `TaskDrawer` 重设计**：Noir 风格，任务状态用语义色（琥珀/哑绿/哑红）

- [ ] **Step 3: 单测 EmptyState/ErrorState 文案渲染**

- [ ] **Step 4: 空态/错误态接入各视图**（HomeView/LibraryView/LogsView 等手写处替换）

- [ ] **Step 5: `npm test` 全绿 + 提交**

```bash
git commit -am "feat(frontend): 空态/错误态/导航项/TaskDrawer"
```

---

## Phase 5 — 视图打磨

### Task 17: 浏览区视图（Home / Movies / Tv / Search / Detail / Favorites / History）

**Files:**
- Modify: `src/views/HomeView.vue`、`src/views/LibraryView.vue`、`src/views/SearchView.vue`、`src/views/MovieView.vue`、`src/views/CollectionView.vue`

- [ ] **Step 1: HomeView**：hero 区去渐变、衬线大标题、海报网格间距放宽

- [ ] **Step 2: LibraryView/SearchView**：`PageHeader` 替换手写 heading，筛选用 `SegmentedControl`，`PosterCard` 换卡

- [ ] **Step 3: MovieView**：详情头部衬线片名、`ResourceList` 替换表、`SegmentedControl` 换筛选、季节选择器统一

- [ ] **Step 4: CollectionView**（收藏/历史）：空态用 `EmptyState`，`PosterCard`

- [ ] **Step 5: `npm test` 全绿 + `npm run build` + 提交**

```bash
git commit -am "style(frontend): 浏览区视图 Noir 打磨"
```

### Task 18: 管理区视图（Settings / Library / Organization / Workflows / Notifications / Logs）

**Files:**
- Modify: `src/views/SettingsView.vue`、`src/views/LibraryWorkbenchView.vue`、`src/views/OrganizationView.vue`、`src/views/OrganizationWorkbenchView.vue`、`src/views/WorkflowCenterView.vue`、`src/views/NotificationCenterView.vue`、`src/views/LogsView.vue`

- [ ] **Step 1: 每视图用 `PageHeader`** 统一标题区

- [ ] **Step 2: SettingsView 拆清晰分组**（连接配置/媒体库/整理/其他），1360 行墙按区块拆，视觉卡片分区

- [ ] **Step 3: 日志/任务/通知** 用等宽字体数据列、语义色状态点

- [ ] **Step 4: `npm test` 全绿 + 提交**

```bash
git commit -am "style(frontend): 管理区视图 Noir 打磨"
```

### Task 19: 登录门

**Files:**
- Modify: `src/App.vue`（auth-gate 区块）

- [ ] **Step 1: 登录门**：居中衬线标题 + 琥珀实底按钮 + 暖炭背景，替换旧样式

- [ ] **Step 2: `npm test` 全绿 + 提交**

```bash
git commit -am "style(frontend): 登录门 Noir 化"
```

---

## Phase 6 — 验证与收尾

### Task 20: 全量验证

- [ ] **Step 1: `npm test`** 全绿

- [ ] **Step 2: `npm run build`** 成功

- [ ] **Step 3: 跑 e2e**（`npx playwright test`，若环境允许；至少核对不因选择器变更而破的冒烟集）

- [ ] **Step 4: 视觉自查**：`npm run dev` 起本地服务，逐视图截图核对（主页/榜单/详情/设置/媒体库），确认无旧蓝紫渐变残留、无排版错乱

- [ ] **Step 5: 更新 `docs/superpowers/specs/2026-08-12-frontend-cinematic-noir-redesign-design.md`** 标注已完成状态

- [ ] **Step 6: 最终提交**

```bash
git commit -am "chore(frontend): Cinematic Noir 重构验证与收尾"
```

---

## 完成标准（对应设计文档第 10 节）

1. 12 个视图全部 Cinematic Noir：暖炭黑 + 琥珀金 + 衬线标题，无渐变/辉光残留。
2. 侧栏剧院布局生效：三组导航、过道灯激活态、响应式三档。
3. `App.vue` 不再承担全部状态；组合式函数 + 布局组件就位。
4. 关键交互行为与重构前一致（测试证明）。
5. `npm test`、`npm run build` 全绿。
