import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第五轮:渠道管理/分页/通知筛选/工作流/返回导航/高级筛选(部署实例 192.168.6.236)", () => {
  test.slow();

  test("通知渠道:创建→列表出现→删除清理(不发送测试消息)", async ({ page }) => {
    const channelName = `E2E 测试渠道 ${Date.now()}`;
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "追更通知" }).click();
    await expect(page.getByRole("heading", { name: "添加通知渠道" })).toBeVisible({ timeout: 30_000 });

    // 填写飞书 Webhook 渠道(格式合法但不指向真实机器人,不点「测试」)
    await page.locator("label", { hasText: "渠道名称" }).locator("input").fill(channelName);
    await page.locator("label", { hasText: "渠道类型" }).locator("select").selectOption("feishu");
    await page.locator("label", { hasText: "飞书 Webhook 地址" }).locator("input").fill("https://example.com/hook/wa-e2e-round5");
    await page.getByRole("button", { name: "添加渠道" }).click();
    await expect(page.getByText("通知渠道已添加")).toBeVisible({ timeout: 15_000 });

    // 列表出现该渠道
    const channelRow = page.locator(".settings-list li", { hasText: channelName }).first();
    await expect(channelRow).toBeVisible({ timeout: 15_000 });

    // 删除清理
    await channelRow.locator('button[aria-label="删除渠道"]').click();
    await expect(channelRow).toHaveCount(0, { timeout: 15_000 });
    await page.screenshot({ ...shot("notify-channel-crud"), fullPage: true });
  });

  test("详情页资源分页:下一页可用时翻页", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });

    const nextBtn = page.getByRole("button", { name: "下一页" });
    if (await nextBtn.isEnabled().catch(() => false)) {
      const firstRow = (await page.locator(".resource-table tbody tr").first().textContent())?.trim() ?? "";
      await nextBtn.click();
      await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
      await expect(page).toHaveURL(/page=2/, { timeout: 30_000 });
      const secondRow = (await page.locator(".resource-table tbody tr").first().textContent())?.trim() ?? "";
      expect(secondRow).not.toBe(firstRow);
    }
    await page.screenshot({ ...shot("resource-pagination"), fullPage: true });
  });

  test("通知中心:全部/未读/可行动筛选切换", async ({ page }) => {
    await page.goto("/notifications");
    await expect(page.locator('[role="tablist"][aria-label="通知筛选"]')).toBeVisible({ timeout: 30_000 });
    for (const label of ["全部", "未读", "需要处理", "错误与安全"]) {
      await page.locator('[role="tablist"][aria-label="通知筛选"] button', { hasText: label }).click();
      // 列表或空态二选一渲染,无致命错误
      await expect(page.locator(".notification-list, .notification-item, .notification-empty").first()).toBeVisible({ timeout: 15_000 });
    }
    await page.screenshot({ ...shot("notification-filters"), fullPage: true });
  });

  test("任务中心只读浏览与返回浏览导航", async ({ page }) => {
    // 任务中心(workflows)渲染
    await page.goto("/workflows");
    await expect(page.locator(".workflow-empty, .workflow-list, .workflow-item, .workflow-card").first()).toBeVisible({ timeout: 30_000 });

    // 返回浏览:目录 → 详情 → 返回按钮回目录
    await page.goto("/movies");
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    await page.locator(".movie-card").first().click();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 60_000 });
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await page.getByRole("button", { name: "返回浏览" }).click();
    await expect(page).toHaveURL(/\/movies/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("back-to-browse"), fullPage: true });
  });

  test("旧路由 /organization-plans 直达可渲染", async ({ page }) => {
    await page.goto("/organization-plans");
    await expect(page.locator("body")).not.toBeEmpty({ timeout: 30_000 });
    await expect(page.locator("nav").first()).toBeVisible({ timeout: 15_000 });
  });

  test("日志高级筛选:事件码筛选只读", async ({ page }) => {
    await page.goto("/logs");
    await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "高级筛选" }).click();
    const eventCodeInput = page.locator("label", { hasText: "事件码" }).locator("input");
    await expect(eventCodeInput).toBeVisible({ timeout: 15_000 });
    await eventCodeInput.fill("task.");
    await eventCodeInput.press("Enter");
    // 列表/空态渲染,无致命错误
    await expect(page.locator(".settings-log-table tbody tr, .settings-empty-block").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".settings-state-error")).toHaveCount(0);
    await page.screenshot({ ...shot("logs-advanced"), fullPage: true });
  });

  test("搜索空输入提交不崩溃", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("searchbox", { name: "搜索电影或电视剧" })).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "提交搜索" }).click();
    await page.waitForTimeout(1500);
    // 页面仍有主导航与内容(未白屏/未崩溃)
    await expect(page.locator("nav").first()).toBeVisible({ timeout: 15_000 });
    const bodyText = await page.locator("body").innerText();
    expect(bodyText.trim().length).toBeGreaterThan(20);
  });

  test("移动端:整理工作台直达渲染", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
    const page = await context.newPage();
    await page.goto("http://192.168.6.236:8115/organization");
    await expect(page.locator("nav").first()).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(1500);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
    await page.screenshot({ ...shot("mobile-organization"), fullPage: true });
  });
});
