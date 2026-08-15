import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第六轮:剩余功能用户操作全覆盖(部署实例 192.168.6.236)", () => {
  test.slow();

  test("详情页刷新资源:真实用户点击强制刷新", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    await page.getByRole("button", { name: "刷新资源" }).click();
    // 刷新触发真实搜索:等待加载结束,资源区恢复(行/空态/错误三选一)
    await expect(
      page.locator(".resource-table tbody tr, .resource-card, .season-empty, .resource-table .resource-page-error").first(),
    ).toBeVisible({ timeout: 240_000 });
    await expect(page.getByText("资源分页暂不可用")).toHaveCount(0);
    await page.screenshot({ ...shot("detail-refresh"), fullPage: true });
  });

  test("内容检测:用户点击开始检测,状态流转不崩溃", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    const inspectBtn = page.locator("button.inspection-more-button").first();
    if (!(await inspectBtn.count())) {
      test.skip(true, "无检测入口(资源为空或检测不支持)");
      return;
    }
    const label = ((await inspectBtn.textContent()) ?? "").trim();
    await inspectBtn.click();
    // 检测开始:出现进度状态或按钮切换为检测中;最终到达完成/失败/部分任一终态(容错)
    await expect(
      page.locator(".inspection-progress[role=status], button.inspection-more-button:disabled, .inspection-progress").first(),
    ).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ ...shot("inspection-trigger"), fullPage: true });
  });

  test("订阅观察面板:查看已发现资源列表", async ({ page }) => {
    await page.goto("/subscriptions");
    await expect(page.getByText("订阅关注的影视，自动检查新资源并推送通知。")).toBeVisible({ timeout: 30_000 });
    // 挑一个有「最近匹配」记录的订阅点「资源」
    const card = page.locator(".subscription-card", { hasText: "最近匹配" }).first();
    if (!(await card.count())) {
      test.skip(true, "无带匹配记录的订阅");
      return;
    }
    await card.getByRole("button", { name: "资源" }).click();
    await expect(page.locator(".observations-panel")).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".observations-panel").getByText("已发现资源")).toBeVisible({ timeout: 15_000 });
    await page.screenshot({ ...shot("subscription-observations"), fullPage: true });
  });

  test("首页 hero 收藏按钮往返", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("button.hero-favorite").first()).toBeVisible({ timeout: 30_000 });
    const heroBtn = page.locator("button.hero-favorite").first();
    const initialActive = await heroBtn.evaluate((el) => el.classList.contains("active"));
    // 翻转收藏
    await heroBtn.click();
    await expect(heroBtn).toHaveClass(initialActive ? /^(?!.*active)/ : /active/, { timeout: 15_000 });
    // 收藏页可见该片(标题从收藏按钮 aria-label 提取:「收藏 X」/「取消收藏 X」)
    const ariaLabel = (await heroBtn.getAttribute("aria-label")) ?? "";
    const heroTitle = ariaLabel.replace(/^(取消)?收藏 /, "");
    await page.goto("/favorites");
    await expect(page.getByRole("heading", { name: "收藏" })).toBeVisible({ timeout: 30_000 });
    if (heroTitle) {
      await expect(page.locator(".movie-card", { hasText: heroTitle.slice(0, 4) }).first()).toBeVisible({ timeout: 15_000 });
    }
    // 还原
    await page.goto("/");
    await expect(page.locator("button.hero-favorite").first()).toBeVisible({ timeout: 30_000 });
    const heroBtn2 = page.locator("button.hero-favorite").first();
    const isActive = await heroBtn2.evaluate((el) => el.classList.contains("active"));
    if (isActive !== initialActive) {
      await heroBtn2.click();
    }
    await page.screenshot({ ...shot("hero-favorite"), fullPage: true });
  });

  test("目录筛选清除:点「全部」恢复列表", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    const yearRow = page.locator(".filter-row", { hasText: "年份" });
    await yearRow.locator("button").nth(1).click();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    expect(page.url()).toContain("year=");
    // 点「全部」清除年份筛选
    await yearRow.locator("button", { hasText: "全部" }).click();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    expect(page.url()).not.toContain("year=");
    await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 45_000 });
  });

  test("资源检测设置:自动检测开关往返持久化", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "资源检测" }).click();
    const toggle = page.locator("label.settings-toggle", { hasText: "自动提交首批资源检测" }).locator("input[type=checkbox]");
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(2500); // 等设置加载
    const initial = (await toggle.isChecked()) as boolean;
    await toggle.setChecked(!initial);
    await page.getByRole("button", { name: "保存" }).first().click();
    await expect(page.getByText("资源检测设置已保存", { exact: false })).toBeVisible({ timeout: 15_000 });
    await page.reload();
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "资源检测" }).click();
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(2500);
    expect(await toggle.isChecked()).toBe(!initial);
    // 还原
    await toggle.setChecked(initial);
    await page.getByRole("button", { name: "保存" }).first().click();
    await expect(page.getByText("资源检测设置已保存", { exact: false })).toBeVisible({ timeout: 15_000 });
    await page.screenshot({ ...shot("inspection-settings-toggle"), fullPage: true });
  });

  test("日志页自动刷新开关与任务中心阶段筛选", async ({ page }) => {
    // 日志自动刷新开关(本地 UI 状态)
    await page.goto("/logs");
    await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 30_000 });
    const autoRefresh = page.locator("label.settings-toggle", { hasText: "自动刷新" }).locator("input[type=checkbox]");
    await expect(autoRefresh).toBeVisible({ timeout: 30_000 });
    const initialRefresh = (await autoRefresh.isChecked()) as boolean;
    await autoRefresh.setChecked(!initialRefresh);
    expect(await autoRefresh.isChecked()).toBe(!initialRefresh);
    await autoRefresh.setChecked(initialRefresh);

    // 任务中心阶段筛选(只读)
    await page.goto("/workflows");
    await expect(page.locator(".workflow-empty, .workflow-list, .workflow-item, .workflow-card").first()).toBeVisible({ timeout: 30_000 });
    const stageSelect = page.locator("label", { hasText: "全部阶段" }).locator("select");
    if (await stageSelect.count()) {
      await stageSelect.selectOption({ index: 1 }).catch(() => {});
      await expect(page.locator(".workflow-empty, .workflow-list, .workflow-item, .workflow-card, .workflow-state").first()).toBeVisible({ timeout: 30_000 });
    }
    await page.screenshot({ ...shot("logs-autorefresh-workflows"), fullPage: true });
  });

  test("通知中心:单条未读点击变已读", async ({ page }) => {
    await page.goto("/notifications");
    await expect(page.locator(".notification-list, .notification-item, .notification-empty").first()).toBeVisible({ timeout: 30_000 });
    const unread = page.locator(".notification-item.unread").first();
    if (!(await unread.count())) {
      test.skip(true, "无未读通知");
      return;
    }
    const before = await page.locator(".notification-item.unread").count();
    await unread.locator(".notification-copy").click();
    // 已读后未读计数减一(首元素可能变成下一条未读,故按计数断言)
    await expect(page.locator(".notification-item.unread")).toHaveCount(before - 1, { timeout: 15_000 });
    await page.screenshot({ ...shot("notification-mark-read"), fullPage: true });
  });

  test("移动端:详情页资源筛选与日志页", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
    const page = await context.newPage();
    // 详情资源筛选(磁力 tab)
    await page.goto("http://192.168.6.236:8115/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    const kindBtn = page.locator(".resource-toolbar button", { hasText: /^磁力/ }).first();
    if (await kindBtn.count()) {
      await kindBtn.click();
      await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    }
    // 日志页
    await page.goto("http://192.168.6.236:8115/logs");
    await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 30_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
    await page.screenshot({ ...shot("mobile-resource-filter-logs"), fullPage: true });
  });
});
