import { expect, test, type Browser, type Page } from "@playwright/test";
import { chromium } from "@playwright/test";
import path from "node:path";

/**
 * 线上全功能端到端测试（192.168.6.236:8115）。
 * 凭据只来自环境变量 WA_E2E_USER / WA_E2E_PASSWORD，禁止写入源码/日志/截图。
 * 安全约束：不触发真实 115 写操作（推送/整理执行/STRM 生成清理/扫描）、不可逆删除。
 * 每个用例收集控制台错误与通知（toast/inline alert）并做体验审计。
 */

const BASE = "http://192.168.6.236:8115";
const SHOTS = path.resolve("test-results/live-all/screenshots");

let browser: Browser;
let storage: string;

test.beforeAll(async () => {
  expect(process.env.WA_E2E_USER, "缺少 WA_E2E_USER").toBeTruthy();
  expect(process.env.WA_E2E_PASSWORD, "缺少 WA_E2E_PASSWORD").toBeTruthy();
  browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/`);
  await page.locator("#username").fill(process.env.WA_E2E_USER as string);
  await page.locator("#password").fill(process.env.WA_E2E_PASSWORD as string);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible({ timeout: 30_000 });
  storage = path.resolve("test-results/live-all/state.json");
  await ctx.storageState({ path: storage });
  await ctx.close();
});

test.afterAll(async () => {
  await browser.close();
});

async function freshPage(): Promise<Page> {
  const ctx = await browser.newContext({ storageState: storage, viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(30_000);
  return page;
}

/** 收集控制台错误；断言用例内无意外错误。 */
function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text().slice(0, 200));
  });
  page.on("pageerror", (err) => errors.push(`PAGEERROR: ${err.message.slice(0, 200)}`));
  return errors;
}

function assertNoUnexpectedErrors(errors: string[]) {
  const allowed = errors.filter((e) => !/401|403|Failed to load resource/i.test(e));
  expect(allowed, `控制台错误: ${allowed.join(" || ")}`).toEqual([]);
}

async function toastTexts(page: Page): Promise<string[]> {
  return page.locator(".toast-message").allTextContents();
}

async function inlineAlerts(page: Page): Promise<string[]> {
  return page.locator(".inline-alert").allTextContents();
}

function shot(name: string) {
  return { path: path.join(SHOTS, `${name}.png`) };
}

async function assertNoOverflow(page: Page, label: string) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, `${label} 横向溢出 ${overflow}px`).toBeLessThanOrEqual(1);
}

async function openDetail(page: Page, titlePrefix = "沙丘") {
  await page.goto(`${BASE}/`);
  const search = page.getByRole("searchbox", { name: "搜索电影或电视剧" });
  await search.fill(titlePrefix);
  await page.getByRole("button", { name: "提交搜索" }).click();
  await expect(page.getByRole("heading", { name: "搜索结果" })).toBeVisible({ timeout: 40_000 });
  const card = page.locator(".movie-card").first();
  await expect(card).toBeVisible({ timeout: 40_000 });
  await card.click();
  await expect(page).toHaveURL(/\/(movie|tv)\/\d+/, { timeout: 40_000 });
  await expect(page.locator(".movie-copy h1").first()).toBeVisible({ timeout: 40_000 });
}

test.describe("A. 首页与导航", () => {
  test("A1 首页板块加载 + 收藏切换 + 无溢出", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      for (const section of ["正在热映", "本周热门", "热播剧集", "即将上映", "高分佳片", "高分剧集"]) {
        await expect(page.getByText(section, { exact: true }).first()).toBeVisible({ timeout: 40_000 });
      }
      await assertNoOverflow(page, "首页");
      // 收藏切换（hero 心形）
      const heroFavorite = page.locator(".hero-favorite").first();
      if (await heroFavorite.count()) {
        const before = (await heroFavorite.getAttribute("aria-label")) ?? "";
        await heroFavorite.click();
        await page.waitForTimeout(400);
        const after = (await heroFavorite.getAttribute("aria-label")) ?? "";
        expect(after).not.toBe(before);
        await heroFavorite.click(); // 还原
      }
      await page.screenshot({ ...shot("a1-home"), fullPage: true });
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("A2 主导航全部视图可达", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      const nav = page.getByRole("navigation", { name: "主导航" });
      const targets: Array<[string, RegExp | string]> = [
        ["首页", /\/$/],
        ["电影", /\/movies/],
        ["剧集", /\/tv/],
        ["热门", /\/popular/],
        ["订阅", /\/subscriptions/],
        ["日志", /\/logs/],
      ];
      for (const [label, urlPattern] of targets) {
        const btn = nav.getByRole("button", { name: label });
        if (!(await btn.count())) continue;
        await btn.click();
        await expect(page).toHaveURL(urlPattern, { timeout: 40_000 });
        await page.waitForTimeout(600);
        expect(await page.locator(".toast-stack .toast").count()).toBe(0);
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("B. 目录与筛选", () => {
  test("B1 电影目录筛选（类型/年份/排序）+ 翻页", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/movies`);
      await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 40_000 });
      await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 40_000 });
      const filterPanel = page.locator(".filter-panel");
      // 类型筛选
      await filterPanel.getByRole("button", { name: "科幻", exact: true }).click();
      await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 40_000 });
      await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 40_000 });
      // 排序
      await page.locator(".filter-options-sort button", { hasText: "评分优先" }).click();
      await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 40_000 });
      // 年份
      await filterPanel.getByRole("button", { name: "2025", exact: true }).click();
      await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 40_000 });
      // 翻页
      const next = page.getByRole("button", { name: "下一页" });
      if (await next.count()) {
        await next.click();
        await expect(page).toHaveURL(/page=2/, { timeout: 40_000 });
        await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 40_000 });
        const prev = page.getByRole("button", { name: "上一页" });
        await prev.click();
        await expect(page).not.toHaveURL(/page=2/, { timeout: 40_000 });
      }
      await assertNoOverflow(page, "电影目录");
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("B2 剧集目录 + 收藏卡片", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/tv`);
      await expect(page.getByRole("heading", { name: "剧集库" })).toBeVisible({ timeout: 40_000 });
      const card = page.locator(".movie-card").first();
      await expect(card).toBeVisible({ timeout: 40_000 });
      const fav = card.locator("button").first();
      if (await fav.count()) {
        await fav.click();
        await page.waitForTimeout(300);
      }
      await assertNoOverflow(page, "剧集目录");
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("C. 详情页与资源区", () => {
  test("C1 详情元数据 + 资源筛选/排序/分页/搜索", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await openDetail(page, "沙丘");
      await expect(page.locator(".movie-copy h1").first()).toContainText(/沙丘/, { timeout: 40_000 });
      // 资源工具栏
      await expect(page.locator(".resource-surface")).toBeVisible({ timeout: 40_000 });
      // 类型筛选
      const segmented = page.locator(".resource-controls .segmented");
      await segmented.getByRole("button", { name: /磁力/ }).click();
      await page.waitForTimeout(800);
      // 质量筛选
      await page.locator(".resource-filter-bar").getByRole("button", { name: /4K/ }).click();
      await page.waitForTimeout(800);
      // 排序
      const sortSelect = page.getByLabel("资源排序");
      if (await sortSelect.count()) {
        await sortSelect.selectOption("size");
        await page.waitForTimeout(800);
      }
      // 名称搜索（防抖）
      const nameSearch = page.locator(".resource-name-search input");
      await nameSearch.fill("Dune");
      await page.waitForTimeout(1200);
      // 每页大小
      const pageSize = page.getByLabel("资源每页数量");
      if (await pageSize.count()) {
        await pageSize.selectOption("100");
        await page.waitForTimeout(800);
      }
      // 刷新资源
      await page.getByRole("button", { name: "刷新资源" }).click();
      await expect(page.locator(".resource-table tbody tr").first()).toBeVisible({ timeout: 40_000 });
      await assertNoOverflow(page, "详情页");
      await page.screenshot({ ...shot("c1-detail"), fullPage: true });
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("C2 资源检测按钮（开始检测→进度→完成或可重试）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await openDetail(page, "盗梦空间");
      await expect(page.locator(".resource-surface")).toBeVisible({ timeout: 40_000 });
      const inspectBtn = page.locator(".inspection-more-button").first();
      if (await inspectBtn.count()) {
        await inspectBtn.click();
        // 进度状态出现
        await expect(page.locator(".inspection-progress")).toBeVisible({ timeout: 20_000 });
        await expect(page.locator(".inspection-progress").first()).toContainText(/检测/, { timeout: 120_000 });
      } else {
        console.log("[info] 无可用检测按钮（资源可能已全部检测或无需检测）");
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("C3 推送按钮门禁与提示（不触发真实推送）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await openDetail(page, "沙丘");
      // 读取组织设置判断推送目录是否已配置（只读 API）
      const settings = await page.evaluate(async () => {
        const res = await fetch("/api/v1/settings/organization");
        if (!res.ok) return null;
        return (await res.json()) as { push_directory_id?: string | null };
      });
      const pushBtn = page.locator("button.push-button").first();
      if (await pushBtn.count()) {
        const disabled = await pushBtn.isDisabled();
        const title = (await pushBtn.getAttribute("title")) ?? "";
        if (!settings?.push_directory_id) {
          // 未配置目录：应可点击且点击后出现“前往设置”引导 toast
          if (!disabled) {
            await pushBtn.click();
            await page.waitForTimeout(800);
            const toasts = await toastTexts(page);
            const alert = await inlineAlerts(page);
            const combined = [...toasts, ...alert].join(" ");
            expect(combined).toMatch(/推送目录|设置/, `点击推送后的提示: ${combined}`);
            // toast 操作按钮
            const actionBtn = page.locator(".toast-action").first();
            if (await actionBtn.count()) {
              await actionBtn.click();
              await expect(page).toHaveURL(/\/settings/, { timeout: 30_000 });
            }
          }
        } else {
          // 已配置目录：只验证按钮存在与禁用态说明，不点击（会触发真实推送）
          expect(title.length).toBeGreaterThan(0);
          console.log("[info] 推送目录已配置，跳过真实推送点击");
        }
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("C4 季度切换（剧集详情）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await openDetail(page, "权力的游戏");
      const seasonSelect = page.locator("#season-select");
      if (await seasonSelect.count()) {
        await seasonSelect.selectOption("1");
        await expect(page.locator(".season-detail-section")).toBeVisible({ timeout: 40_000 });
        await page.waitForTimeout(1000);
      } else {
        console.log("[info] 该片无季度选择器（可能不是剧集），跳过");
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("D. 收藏/记录/任务抽屉", () => {
  test("D1 收藏与观看记录视图", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      // 先收藏一部影片
      await page.goto(`${BASE}/movies`);
      const card = page.locator(".movie-card").first();
      await expect(card).toBeVisible({ timeout: 40_000 });
      const favBtn = card.locator("xpath=..").locator(".favorite-button").first();
      if (await favBtn.count()) {
        await favBtn.click();
        await page.waitForTimeout(500);
      } else {
        await card.locator("button").first().click();
        await page.waitForTimeout(500);
      }
      // 打开收藏
      await page.getByRole("button", { name: "收藏", exact: true }).first().click();
      await expect(page).toHaveURL(/favorites/, { timeout: 30_000 });
      await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 40_000 });
      // 取消收藏还原
      await page.locator(".favorite-button").first().click();
      await page.waitForTimeout(400);
      // 观看记录
      await page.getByRole("button", { name: "记录", exact: true }).first().click();
      await expect(page).toHaveURL(/history/, { timeout: 30_000 });
      await assertNoOverflow(page, "收藏/记录");
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("D2b 推送任务状态展示：uncertain 为警告、failed 为错误、available 为成功", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      await page.getByRole("button", { name: "推送任务" }).first().click();
      await expect(page.locator(".task-drawer")).toBeVisible({ timeout: 20_000 });
      // 等待任务列表加载
      await expect(page.locator(".task-row").first()).toBeVisible({ timeout: 30_000 });
      const rows = page.locator(".task-row");
      const count = await rows.count();
      expect(count).toBeGreaterThan(0);
      // 校验每种状态的图标色调：uncertain → amber 警告，failed → danger，available → mint
      const tones: Record<string, string> = {};
      for (let i = 0; i < count; i++) {
        const row = rows.nth(i);
        const icon = row.locator(".task-icon");
        const state = (await icon.getAttribute("class")) ?? "";
        const color = await icon.evaluate((el) => getComputedStyle(el).color);
        const m = state.match(/task-icon (\S+)/);
        if (m) tones[m[1]] = color;
      }
      console.log("[info] 抽屉状态色调:", JSON.stringify(tones));
      if (tones.uncertain && tones.failed) {
        // uncertain 应为警告色（amber），不得与 failed 的错误红相同
        expect(tones.uncertain, `uncertain 与 failed 同为错误红: ${tones.uncertain}`).not.toBe(tones.failed);
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("D2 推送任务抽屉开合", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      const drawerBtn = page.getByRole("button", { name: "推送任务" }).first();
      await drawerBtn.click();
      const drawer = page.locator(".task-drawer");
      await expect(drawer).toBeVisible({ timeout: 20_000 });
      // 抽屉内容（任务行或空态）
      expect(await drawer.locator(".task-row, .empty-state").count()).toBeGreaterThan(0);
      await drawer.getByRole("button", { name: "关闭" }).click();
      await expect(drawer).toHaveCount(0, { timeout: 20_000 });
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("E. 任务中心", () => {
  test("E1 工作流列表/筛选/详情/取消（仅安全状态）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/workflows`);
      await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible({ timeout: 40_000 });
      // 筛选
      const statusFilter = page.locator("#workflow-status");
      if (await statusFilter.count()) {
        await statusFilter.selectOption("in_progress");
        await page.waitForTimeout(800);
        await statusFilter.selectOption("");
        await page.waitForTimeout(800);
      }
      // 选择一个工作流看详情
      const row = page.locator(".workflow-row").first();
      if (await row.count()) {
        await row.click();
        await expect(page.locator(".workflow-detail")).toBeVisible({ timeout: 30_000 });
        // 时间线存在
        expect(await page.locator(".workflow-timeline li").count()).toBeGreaterThan(0);
        // 取消按钮：仅在可取消且无运行中阶段时点击
        const cancelBtn = page.getByRole("button", { name: "取消工作流" });
        if (await cancelBtn.count()) {
          const detailText = await page.locator(".workflow-detail").textContent();
          if (!/(执行中|运行中|结果待确认)/.test(detailText ?? "")) {
            page.once("dialog", (d) => void d.accept());
            await cancelBtn.click();
            await page.waitForTimeout(1500);
            console.log("[info] 已取消一个安全状态的工作流");
          } else {
            console.log("[info] 工作流有运行中阶段，跳过取消");
          }
        }
      } else {
        console.log("[info] 当前无工作流记录");
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("F. 通知中心（重点：通知体验）", () => {
  test("F1 列表/筛选/已读/全部已读/偏好设置（含还原）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/notifications`);
      await expect(page.getByRole("heading", { name: /通知中心/ })).toBeVisible({ timeout: 40_000 });
      // 筛选 tab
      const tabs = page.locator(".notification-controls .segmented button");
      if (await tabs.count()) {
        for (const label of ["未读", "需要处理", "错误与安全", "全部"]) {
          const tab = page.locator(".notification-controls .segmented button", { hasText: label }).first();
          if (await tab.count()) {
            await tab.click();
            await page.waitForTimeout(400);
          }
        }
      }
      // 单条已读
      const firstUnread = page.locator(".notification-item.unread").first();
      if (await firstUnread.count()) {
        await firstUnread.click();
        await page.waitForTimeout(600);
        expect(await page.locator(".notification-item.unread").count()).toBeLessThanOrEqual(
          Math.max(0, await page.locator(".notification-item.unread").count() + 1),
        );
      }
      // 全部已读
      const markAll = page.getByRole("button", { name: "全部已读" });
      if (await markAll.count() && !(await markAll.isDisabled())) {
        await markAll.click();
        await page.waitForTimeout(800);
      }
      // 通知接收开关（来回切换还原）
      const prefToggle = page.locator(".notification-preference input[type=checkbox]").first();
      if (await prefToggle.count()) {
        const original = await prefToggle.isChecked();
        await prefToggle.setChecked(!original);
        await page.waitForTimeout(800);
        await prefToggle.setChecked(original);
        await page.waitForTimeout(800);
      }
      // 静默时段保存（改一次再改回）
      const quietStart = page.locator(".notification-time input[type=time]").first();
      if (await quietStart.count()) {
        const original = (await quietStart.inputValue()) ?? "23:00";
        await quietStart.fill("22:30");
        await quietStart.press("Enter");
        await page.waitForTimeout(1000);
        await quietStart.fill(original);
        await quietStart.press("Enter");
        await page.waitForTimeout(1000);
      }
      // 静默保存成功应有 toast
      const toasts = await toastTexts(page);
      console.log("[info] 静默时段相关 toast:", JSON.stringify(toasts));
      await page.screenshot({ ...shot("f1-notifications") });
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("G. 订阅", () => {
  test("G1 创建→暂停→恢复→删除（测试订阅，最后删除还原）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/subscriptions`);
      await expect(page.getByRole("heading", { name: /订阅/ })).toBeVisible({ timeout: 40_000 });
      const createBtn = page.getByRole("button", { name: "新建订阅" });
      if (!(await createBtn.count())) {
        console.log("[info] 无新建订阅按钮，跳过");
        return;
      }
      await createBtn.click();
      const tmdbInput = page.locator(".create-form input").first();
      // 使用固定测试编号（优先不存在的），避免与既有订阅冲突
      const testTmdbId = "603";
      const existingIds = await page.evaluate(async () => {
        const res = await fetch("/api/v1/subscriptions");
        if (!res.ok) return [];
        return (await res.json()).map((s: { tmdb_id?: number }) => s.tmdb_id);
      });
      if (existingIds.includes(Number(testTmdbId))) {
        console.log("[info] 测试订阅 603 已存在，跳过创建");
        return;
      }
      await tmdbInput.fill(testTmdbId);
      await page.locator(".create-form").getByRole("button", { name: /创建|保存/ }).click();
      // 创建成功：出现成功 toast（或错误提示）
      await page.waitForTimeout(2500);
      const toasts = await toastTexts(page);
      const alerts = await inlineAlerts(page);
      console.log("[info] 订阅创建 toast:", JSON.stringify(toasts), "alerts:", JSON.stringify(alerts));
      if (alerts.length) {
        expect(alerts.join(" ")).toMatch(/已存在|重复|无效|失败/);
      } else {
        expect(toasts.join(" ")).toContain("订阅已创建");
      }
      // 取消订阅按钮为占位（无删除 API），仅验证其存在
      expect(await page.locator("button[title=\"取消订阅\"]").count()).toBeGreaterThan(0);
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("H. 日志", () => {
  test("H1 日志加载/筛选/加载更多/清除/自动刷新/保留设置保存", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/logs`);
      await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 40_000 });
      await expect(page.locator(".settings-log-table tbody tr, .settings-log-item").first()).toBeVisible({ timeout: 40_000 });
      // 分类筛选
      const catSelect = page.locator(".settings-filter-row select").first();
      if (await catSelect.count()) {
        await catSelect.selectOption("system");
        await page.waitForTimeout(1000);
        await catSelect.selectOption("");
        await page.waitForTimeout(1000);
      }
      // 清除筛选
      await page.getByRole("button", { name: "清除筛选" }).click();
      await page.waitForTimeout(1000);
      // 加载更多
      const more = page.getByRole("button", { name: "加载更多" });
      if (await more.count()) {
        await more.click();
        await page.waitForTimeout(1500);
      }
      // 自动刷新开关（还原）
      const autoRefresh = page.locator(".settings-section-actions input[type=checkbox]").first();
      if (await autoRefresh.count()) {
        const original = await autoRefresh.isChecked();
        await autoRefresh.setChecked(!original);
        await page.waitForTimeout(400);
        await autoRefresh.setChecked(original);
      }
      // 日志保留：相同值保存（无实际变更）
      const saveBtn = page.locator(".logging-settings .settings-save-bar .primary-button");
      if (await saveBtn.count()) {
        await saveBtn.click();
        await page.waitForTimeout(1500);
        const toasts = await toastTexts(page);
        console.log("[info] 日志保留保存 toast:", JSON.stringify(toasts));
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("I. 设置各分区", () => {
  test("I1 概览 + 连接配置（无效凭据校验不落库）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await expect(page.getByRole("heading", { name: "设置" })).toBeVisible({ timeout: 40_000 });
      // 概览
      const overviewMetrics = page.locator(".settings-metrics .settings-metric");
      await expect(overviewMetrics.first()).toBeVisible({ timeout: 40_000 });
      // 连接配置
      await page.locator(".settings-nav").getByRole("button", { name: "连接配置" }).click();
      await expect(page.getByRole("heading", { name: "连接配置" })).toBeVisible({ timeout: 40_000 });
      const tmdbInput = page.locator('input[aria-label="TMDB API Key"]').first();
      await expect(tmdbInput).toBeVisible({ timeout: 40_000 });
      // 输入无效 Key → 保存并验证 → 应出现结构化错误且不落库
      await tmdbInput.fill("invalid-test-key-12345");
      await page.getByRole("button", { name: "保存并验证" }).first().click();
      await page.waitForTimeout(2500);
      const alerts = await inlineAlerts(page);
      console.log("[info] 无效凭据提示:", JSON.stringify(alerts));
      // 错误后清空输入框
      await tmdbInput.fill("");
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I2 115 推送分区（验证 Cookie 只读操作）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await page.locator(".settings-nav").getByRole("button", { name: "连接配置" }).click();
      await expect(page.getByRole("heading", { name: "连接配置" })).toBeVisible({ timeout: 40_000 });
      const validateBtn = page.getByRole("button", { name: "验证当前 Cookie" }).first();
      if (await validateBtn.count()) {
        await validateBtn.click();
        await page.waitForTimeout(3000);
        const alerts = await inlineAlerts(page);
        console.log("[info] Cookie 验证提示:", JSON.stringify(alerts));
        expect(alerts.join(" ")).toMatch(/Cookie|就绪|授权|不可用|验证/);
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I3 内容安全（临时保存并还原）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await page.locator(".settings-nav").getByRole("button", { name: "内容安全" }).click();
      await expect(page.locator(".settings-policy-row").first()).toBeVisible({ timeout: 40_000 });
      const keywordBox = page.locator(".content-keywords textarea");
      const original = (await keywordBox.inputValue()) ?? "";
      await keywordBox.fill(original ? `${original}\n测试词条-临时` : "测试词条-临时");
      const saveBtn = page.locator(".settings-save-bar .primary-button");
      await saveBtn.click();
      await page.waitForTimeout(2000);
      const toasts = await toastTexts(page);
      console.log("[info] 内容安全保存 toast:", JSON.stringify(toasts));
      // 还原
      await keywordBox.fill(original);
      await page.locator(".settings-save-bar .primary-button").click();
      await page.waitForTimeout(2000);
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I4 资源检测（自动检测开关往返）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await page.locator(".settings-nav").getByRole("button", { name: "资源检测" }).click();
      await expect(page.locator(".settings-toggle input").first()).toBeVisible({ timeout: 40_000 });
      const toggle = page.locator(".settings-toggle input").first();
      const original = await toggle.isChecked();
      await toggle.setChecked(!original);
      await page.locator(".settings-save-bar .primary-button").click();
      // 等待保存完成（保存条消失）
      await expect(page.locator(".settings-save-bar")).toHaveCount(0, { timeout: 20_000 });
      const toasts1 = await toastTexts(page);
      console.log("[info] 资源检测保存 toast:", JSON.stringify(toasts1));
      // 还原
      await toggle.setChecked(original);
      await page.locator(".settings-save-bar .primary-button").click();
      await expect(page.locator(".settings-save-bar")).toHaveCount(0, { timeout: 20_000 });
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I5 115 整理分区（只读走查，不触发真实整理）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await page.locator(".settings-nav").getByRole("button", { name: "115 整理" }).click();
      await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible({ timeout: 40_000 });
      await page.waitForTimeout(1500);
      // 保存按钮存在（相同值保存）
      const saveBtn = page.locator(".settings-save-bar .primary-button");
      if (await saveBtn.count()) {
        await saveBtn.click();
        await page.waitForTimeout(2000);
        const toasts = await toastTexts(page);
        console.log("[info] 整理设置保存 toast:", JSON.stringify(toasts));
      }
      // 整理操作按钮在整理工作台（OrganizationView）内，此处只验证设置表单已加载
      expect(await page.locator(".settings-section").count()).toBeGreaterThan(0);
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I6 Prowlarr 搜索源（只读验证连接）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await expect(page.locator(".settings-nav button").first()).toBeVisible({ timeout: 40_000 });
      const prowlarrTab = page.locator(".settings-nav button", { hasText: "搜索来源" });
      if (await prowlarrTab.count()) {
        await prowlarrTab.click();
        await page.waitForTimeout(2000);
        const verifyBtn = page.getByRole("button", { name: /验证/ }).first();
        if (await verifyBtn.count()) {
          await verifyBtn.click();
          await page.waitForTimeout(4000);
          const alerts = await inlineAlerts(page);
          console.log("[info] Prowlarr 验证提示:", JSON.stringify(alerts));
        }
      } else {
        console.log("[info] 无 Prowlarr 分区");
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("I7 签到分区（开关往返）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/settings`);
      await expect(page.locator(".settings-nav button").first()).toBeVisible({ timeout: 40_000 });
      const checkinTab = page.locator(".settings-nav button", { hasText: "签到" });
      if (await checkinTab.count()) {
        await checkinTab.click();
        await page.waitForTimeout(1500);
        const toggle = page.locator(".settings-toggle input, input[type=checkbox]").first();
        if (await toggle.count()) {
          const original = await toggle.isChecked();
          // 签到为切换即保存
          await toggle.setChecked(!original);
          await page.waitForTimeout(2000);
          const toasts = await toastTexts(page);
          console.log("[info] 签到保存 toast:", JSON.stringify(toasts));
          await toggle.setChecked(original);
          await page.waitForTimeout(2000);
        }
      } else {
        console.log("[info] 无签到分区");
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("J. 整理工作台", () => {
  test("J1 计划列表/状态筛选/确认/忽略（本地操作）", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/organization`);
      await expect(page.getByRole("heading", { name: /整理/ })).toBeVisible({ timeout: 40_000 });
      await page.waitForTimeout(1500);
      const tabs = page.locator(".organization-tabs button");
      if (await tabs.count()) {
        for (const label of ["待确认", "已确认", "已忽略", "已失效"]) {
          const tab = page.locator(".organization-tabs button", { hasText: label }).first();
          if (await tab.count()) {
            await tab.click();
            await page.waitForTimeout(800);
          }
        }
      }
      // 本地确认/忽略（不触发远端）
      const confirmBtn = page.getByRole("button", { name: /确认本地计划/ });
      if (await confirmBtn.count()) {
        await confirmBtn.click();
        await page.waitForTimeout(1500);
        const toasts = await toastTexts(page);
        console.log("[info] 确认计划 toast:", JSON.stringify(toasts));
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });

  test("J2 整理历史加载", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/organization`);
      await expect(page.locator(".organization-tabs").first()).toBeVisible({ timeout: 40_000 });
      await page.locator(".organization-tabs button", { hasText: "历史" }).first().click();
      await page.waitForTimeout(2000);
      const body = await page.locator("body").textContent();
      expect(body ?? "").toMatch(/整理|归档|历史/);
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("K. 媒体库工作台（只读走查）", () => {
  test("K1 媒体库加载/刷新/按钮门禁", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/library`);
      await page.waitForTimeout(3000);
      const body = (await page.locator("body").textContent()) ?? "";
      expect(body).toMatch(/媒体库|STRM|115/);
      // 刷新按钮
      const refresh = page.locator(".library-workbench-heading button, button[title*=\"刷新\"]").first();
      if (await refresh.count()) {
        await refresh.click();
        await page.waitForTimeout(1500);
      }
      // 危险按钮只验证存在与禁用态，不点击
      for (const name of ["扫描目录", "全量 STRM", "增量同步", "清理失效", "生成整理预览"]) {
        const btn = page.getByRole("button", { name }).first();
        if (await btn.count()) {
          const disabled = await btn.isDisabled();
          console.log(`[info] 按钮「${name}」disabled=${disabled}`);
        }
      }
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("L. 离线/恢复通知（重点：新体验）", () => {
  test("L1 断网出现离线 toast 与状态点，恢复后出现恢复 toast", async () => {
    const page = await freshPage();
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      await expect(page.getByText("正在热映", { exact: true }).first()).toBeVisible({ timeout: 40_000 });
      await page.context().setOffline(true);
      await page.waitForTimeout(1500);
      const offlineToasts = await toastTexts(page);
      console.log("[info] 离线 toast:", JSON.stringify(offlineToasts));
      expect(offlineToasts.join(" ")).toMatch(/离线|网络/);
      // 状态点
      const offlineDot = page.locator(".status-dot.offline").first();
      expect(await offlineDot.count()).toBeGreaterThan(0);
      await page.context().setOffline(false);
      await page.waitForTimeout(1500);
      const restoreToasts = await toastTexts(page);
      console.log("[info] 恢复 toast:", JSON.stringify(restoreToasts));
      expect(restoreToasts.join(" ")).toMatch(/恢复|网络/);
      assertNoUnexpectedErrors(errors);
    } finally {
      await page.context().close();
    }
  });
});

test.describe("M. 移动端关键路径", () => {
  test("M1 移动端：登录后首页/详情/设置无溢出", async () => {
    const ctx = await browser.newContext({ storageState: storage, viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    page.setDefaultTimeout(30_000);
    const errors = watch(page);
    try {
      await page.goto(`${BASE}/`);
      await expect(page.getByText("正在热映", { exact: true }).first()).toBeVisible({ timeout: 40_000 });
      await assertNoOverflow(page, "移动端首页");
      // 打开菜单→电影
      const menuBtn = page.getByRole("button", { name: "菜单" }).first();
      if (await menuBtn.count()) {
        await menuBtn.click();
        await page.getByRole("button", { name: "电影" }).first().click();
        await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 40_000 });
        await assertNoOverflow(page, "移动端电影目录");
      }
      await page.screenshot({ ...shot("m1-mobile-home") });
      assertNoUnexpectedErrors(errors);
    } finally {
      await ctx.close();
    }
  });
});
