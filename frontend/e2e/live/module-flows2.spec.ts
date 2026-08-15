import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第三轮:深链接/导航/设置分区/移动端扩展(部署实例 192.168.6.236)", () => {
  test.slow();

  test("深链接直达剧集详情并刷新保持", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page).toHaveURL(/\/tv\/1399/);
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".movie-copy h1")).toContainText("权力的游戏", { timeout: 60_000 });
    // 资源区最终稳定(行/空态/错误三选一)
    await expect(
      page.locator(".resource-table tbody tr, .resource-card, .season-empty, .resource-table .resource-page-error").first(),
    ).toBeVisible({ timeout: 240_000 });

    // 刷新后 URL 与详情保持
    await page.reload();
    await expect(page).toHaveURL(/\/tv\/1399/);
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ ...shot("deep-link-tv"), fullPage: true });
  });

  test("浏览器后退/前进在目录与详情间往返", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    const firstTitle = (await page.locator(".movie-card .movie-card-copy strong").first().textContent())?.trim();

    await page.locator(".movie-card").first().click();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 60_000 });
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });

    await page.goBack();
    await expect(page).toHaveURL(/\/movies/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-card .movie-card-copy strong").first()).toHaveText(firstTitle ?? "", { timeout: 30_000 });

    await page.goForward();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 30_000 });
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ ...shot("history-nav"), fullPage: true });
  });

  test("PWA manifest 与入口可用", async ({ page }) => {
    const resp = await page.request.get("/manifest.webmanifest");
    expect(resp.status()).toBe(200);
    const manifest = (await resp.json()) as { name?: string; start_url?: string; display?: string };
    expect(manifest.name).toBeTruthy();
    expect(manifest.start_url).toBeTruthy();
    const userjs = await page.request.get("/watch-assistant.user.js");
    expect(userjs.status()).toBe(200);
  });

  test("设置剩余分区:内容安全/资源检测/连接配置/签到/追更通知可浏览", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    const cases: Array<[string, string]> = [
      ["内容安全", "内容安全"],
      ["资源检测", "资源检测"],
      ["连接配置", "连接配置"],
      ["115 自动签到", "115 自动签到"],
      ["追更通知", "追更通知"],
    ];
    for (const [navLabel, headingText] of cases) {
      await page.locator(".settings-nav button", { hasText: navLabel }).click();
      await expect(page.getByRole("heading", { name: headingText }).first()).toBeVisible({ timeout: 30_000 });
    }
    // 115 自动签到分区含状态展示(只读)
    await page.locator(".settings-nav button", { hasText: "115 自动签到" }).click();
    await expect(page.locator(".settings-section").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("settings-sections"), fullPage: true });
  });

  test("移动端:订阅管理与详情订阅按钮可用", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
    const page = await context.newPage();
    await page.goto("http://192.168.6.236:8115/subscriptions");
    await page.goto("/subscriptions");
    await expect(page.getByText("订阅关注的影视，自动检查新资源并推送通知。")).toBeVisible({ timeout: 30_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);

    // 移动端进电影详情,订阅按钮可见
    await page.goto("/movies");
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    await page.locator(".movie-card").first().click();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 60_000 });
    await expect(page.locator("button.subscribe-inline")).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ ...shot("mobile-detail-subscribe"), fullPage: true });
  });

  test("移动端:设置分区选择器切换 115 整理", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
    const page = await context.newPage();
    await page.goto("http://192.168.6.236:8115/settings");
    await page.goto("/settings");
    await expect(page.locator(".settings-mobile-select select")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-mobile-select select").selectOption("organization");
    await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible({ timeout: 30_000 });
    await page.locator("details.settings-subsection summary", { hasText: "高级设置" }).click();
    const toggle = page
      .locator("label.settings-toggle", { hasText: "自动清理广告垃圾文件" })
      .locator("input[type=checkbox]");
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("mobile-settings-org"), fullPage: true });
  });

  test("整理工作台:视图切换只读浏览无错误", async ({ page }) => {
    await page.goto("/organization");
    await expect(page.getByRole("heading", { name: "整理" }).first()).toBeVisible({ timeout: 60_000 });
    for (const tab of ["待处理", "已确认", "历史"]) {
      const tabBtn = page.locator('[role="tab"], .segmented button', { hasText: tab }).first();
      if (await tabBtn.count()) {
        await tabBtn.click();
        await page.waitForTimeout(800);
      }
    }
    // 页面不得出现致命错误
    await expect(page.getByText("页面暂时无法加载").first()).toHaveCount(0, { timeout: 10_000 });
    await page.screenshot({ ...shot("organization-workbench"), fullPage: true });
  });
});
