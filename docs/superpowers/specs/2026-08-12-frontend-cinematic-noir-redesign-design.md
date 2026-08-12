# 前端 Cinematic Noir 重构设计

- 日期：2026-08-12
- 范围：Watch Assistant 前端（Vue 3 / TypeScript / Vite）
- 状态：已通过用户两轮确认

## 1. 背景与目标

用户对当前前端不满意：信息太杂乱且不美观。现状问题：

- `App.vue` 1692 行单体，状态、API 调用、布局、全部视图逻辑堆在一个文件。
- 顶栏过载：品牌 + 分组导航 + 搜索框 + 5 个快捷图标按钮，信息密度过高。
- `styles.css` 1241 行全局样式；`SettingsView` 1360 行、`LibraryWorkbenchView` 978 行。
- 视觉噪点多：大量渐变、辉光、彩色徽章、胶囊按钮。

目标：以「影院级深色（Cinematic Noir）」美学重设计全部 12 个视图，并同步拆解超大
文件、组件化、建立设计令牌系统，做到**信息克制、视觉统一、可长期维护**，且**行为零
变更**。

## 2. 决策记录

| 决策 | 选项 | 结论 |
|---|---|---|
| 范围 | 视觉重设计 + 代码重构 / 仅视觉 / 分阶段 | **视觉重设计 + 代码重构** |
| 美学方向 | A 影院级深色 / B 明亮画廊 / C 剧场霓虹 | **A · Cinematic Noir** |
| 打磨重点 | 浏览优先 / 后台优先 / 全部均衡 | **全部 12 视图深度打磨** |
| 技术栈 | 零新依赖手写 CSS / 允许组件库 | **零新依赖，手写 CSS 设计令牌** |
| 信息架构 | 侧栏剧院 / 极简顶部 / 分屏工作台 | **侧栏剧院** |

## 3. 设计令牌（Design Tokens）

### 3.1 调色板

暖炭黑家族（去掉冷蓝），全站**唯一强调色**为影院琥珀金。

```css
--bg            #14120F;  /* 暖炭黑根底 */
--bg-soft       #1A1714;  /* 次级底 */
--surface       #201C18;  /* 卡片面 */
--surface-2     #28231E;  /* 抬高面 */
--border        #2E2923;  /* 发丝线 */
--border-strong #3D362C;

--text          #F2ECE1;  /* 暖象牙白 */
--text-2        #A89F91;  /* 次级 */
--text-3        #6C6455;  /* 弱化 */

--accent        #D8A45E;  /* 影院琥珀金 */
--accent-hover  #E6BD80;
--accent-soft   rgb(216 164 94 / 12%);

--success  #8FB078;  /* 哑绿，降饱和 */
--danger   #C96B5B;  /* 哑红，降饱和 */
--warning  #D8A45E;  /* 复用琥珀 */
--rating   #E0B974;  /* 评分金 */
```

纪律：发丝边框 + 单一柔和阴影；去掉渐变按钮、辉光、彩色徽章堆叠。

### 3.2 字体（全系统栈，零新依赖）

| 角色 | 字体栈 | 用途 |
|---|---|---|
| 展示 Display | `Iowan Old Style, Songti SC, Noto Serif CJK SC, Georgia, serif` | 页面大标题、电影名、空态引导——海报气质 |
| 正文 Body | `Inter, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif` | 界面控件、说明文字 |
| 数据 Utility | `ui-monospace, SF Mono, JetBrains Mono, Menlo, monospace` | 日志、大小、infohash、时间戳 |

### 3.3 圆角、阴影、签名元素

- 圆角收敛到 6px（去掉 999px 胶囊按钮、去圆形评级盘），几何精确感。
- 阴影收敛：细边框 + 单一柔和投影，去掉 glow。
- **签名元素「过道灯」**：侧栏当前页左侧一条琥珀灯管竖线；海报 hover 时边框亮起
  琥珀光。全站唯一"发光"的地方就是它，其余全部安静。

## 4. 信息架构与布局

侧栏剧院：左侧持久侧栏分组导航 + 薄顶栏。

```
┌─────────┬────────────────────────────────────────┐
│ WATCH    │  ⌕ 搜索电影或剧集               ● 在线  │
│ ASSIST   │  （顶栏只剩一行：搜索 + 状态）          │
│───────── │─────────────────────────────────────── │
│ 发现     │                                        │
│  · 首页  │      奥本海默                          │
│  · 电影  │      2023 · 传记 · 8.9                 │
│  · 剧集  │      ┌────┐  ┌────┐  ┌────┐           │
│  · 搜索  │      │海报│  │海报│  │海报│           │
│ 我的     │      └────┘  └────┘  └────┘           │
│  · 收藏  │                                        │
│  · 历史  │                                        │
│ 管理     │                                        │
│  · 媒体库│                                        │
│  · 整理  │                                        │
│  · 任务中心│                                      │
│  · 通知  │  ▍ 琥珀过道灯（当前页）                │
│  · 日志  │                                        │
│  · 设置  │                                        │
└─────────┴────────────────────────────────────────┘
```

- 侧栏 240px：品牌置顶 + 三组导航（发现/我的/管理）+ 底部连接状态。
- 顶栏从 5 个快捷按钮减到 0；任务中心/通知/日志/设置收纳进「管理」组。
- 响应式：>1200px 完整侧栏；768–1200px 收成图标栏；<768px 抽屉式侧栏。
- 内容区最大宽度 ~1440px；海报网格间距放宽，大段留白。

## 5. 组件与视图重构

### 5.1 `App.vue`（1692 行）拆解

布局层：

```
src/layout/
  AppShell.vue       # 侧栏 + 顶栏 + 内容区网格
  AppSidebar.vue     # 品牌 + 三组导航 + 过道灯 + 底部连接状态
  AppTopbar.vue      # 全局搜索 + 在线状态
```

状态层（组合式函数）：

```
src/composables/
  useAuth.ts                 # 登录/会话
  useConnectivity.ts         # 在线/离线
  useCatalog.ts              # 首页榜单 + 电影/剧集发现 + 分页 + 筛选
  useSearch.ts               # 全局搜索 + 结果状态
  useMediaDetail.ts          # 详情页状态机：元数据/季/资源分页/检查/推送
  useFavoritesHistory.ts     # 收藏/历史（本地存储）
  useTaskDrawer.ts           # 任务抽屉
  useCapabilities.ts         # 各能力开关状态
```

### 5.2 共享原语组件

| 新组件 | 替代 | 说明 |
|---|---|---|
| `PosterCard.vue` | `MovieCard.vue` | 海报为主角，衬线片名，hover 琥珀边光 |
| `PageHeader.vue` | 各 view 手写 heading | 衬线大标题 + eyebrow + 操作区 |
| `ResourceList.vue` | `ResourceTable.vue` | 干净列表，数据列等宽字体，徽章最少 |
| `SegmentedControl.vue` | 渐变胶囊按钮 | 资源 kind/quality/sort 筛选 |
| `EmptyState.vue` / `ErrorState.vue` | 各 view 手写 | 统一语调与排版 |
| `MarqueeNavItem.vue` | — | 侧栏导航项，激活态带过道灯条 |
| `TaskDrawer.vue`（重设计） | `TaskDrawer.vue` | 任务抽屉，Noir 风格重做 |

样式组织：`tokens.css`（CSS 变量）+ `base.css`（reset/排版）+ `primitives.css`
（共享原语），视图细节用 SFC scoped 样式。替代现有 1241 行单文件。

### 5.3 视图处理

- 浏览区（首页/电影/剧集/搜索/详情）：衬线大标题、大留白海报墙、最小 chrome。
- 管理区（设置/媒体库/整理/任务/通知/日志）：统一 PageHeader + 卡片分区；
  设置页从 1360 行墙拆成清晰分组。
- 登录门（auth gate）同样采用 Noir 风格：居中衬线标题 + 琥珀按钮，不再是独立旧样式。

## 6. 数据流与状态

- `api.ts` 一行不动，API 契约稳定。
- 组合式函数各自持有状态，视图绑定 ref + 发事件；跨视图通信保持现状。
- **严格零行为变更**：路由、深链、轮询、离线回退、检查/推送逻辑全部原样保留，
  只换外观和结构。现有自定义 history 路由（`router.ts`）继续沿用，不引入 vue-router。

## 7. 错误 / 空态 / 加载态（Noir 语感）

- 空态：衬线引导语 + 行动建议——「这里还空着，去首页看看今天的片单」。
- 错误：界面口吻说明发生了什么 + 怎么修复，不道歉不模糊。
- 加载：海报骨架屏 + 缓慢暖色 shimmer。
- 离线：琥珀色提示条（暖提醒，非红色警报）。

## 8. 测试与验证

- 先盘点现有 vitest + Testing Library + Playwright e2e + MSW，全部保住。
- 组件 API 变更（如 `MovieCard` → `PosterCard` 的 props）同步更新单测。
- 新增测试：侧栏分组结构、`useCatalog` / `useMediaDetail` 状态迁移、`PosterCard` 渲染。
- 完成标准：`npm test` + `npm run build` + `npm run test:e2e` 全绿。

## 9. 非目标

- 不引入新依赖（含组件库、CSS 框架、字体 CDN）。
- 不改变后端 API、路由、数据契约。
- 不做行为增强（不加新功能、不改轮询/推送逻辑）。
- 不改动 TMDB 用户脚本（`userscript.*`）。

## 11. 实施偏差记录（2026-08-12 实施时）

1. **组合式函数范围调整**：提取了 `useAuth`、`useConnectivity`、`useFavoritesHistory`、
   `useCapabilities` 四个可独立单元。`useCatalog`/`useSearch`/`useMediaDetail` 因与
   路由恢复、滚动位置、详情返回、共享 `requestCatalog` 管道深度耦合，在"行为零变更"
   约束下保留为 App.vue 的控制器职责，未强行拆分。
2. **ResourceList 未重写**：`ResourceTable` 结构复杂（检查/推送/筛选/分页），改为
   CSS 抛光（等宽数据列、降噪徽章、语义色），未做组件级重写以避免回归。
3. **PosterCard 已替换 MovieCard**：海报为主角、琥珀评分小字、hover 琥珀边光。
4. **侧栏过道灯**：作为激活态内联实现于 `AppSidebar.vue`（`.sidebar-item-light`），
   未单独抽 `MarqueeNavItem` 组件。

## 10. 验收标准

1. 12 个视图全部呈现 Cinematic Noir 风格：暖炭黑 + 琥珀金 + 衬线标题，无渐变/辉光残留。
2. 侧栏剧院布局生效：三组导航、过道灯激活态、响应式三档。
3. `App.vue` 不再承担全部状态；组合式函数 + 布局组件就位。
4. 关键交互（搜索、详情、资源筛选、推送、任务抽屉、离线横幅）行为与重构前一致。
5. `npm test`、`npm run build`、`npm run test:e2e` 全绿。
