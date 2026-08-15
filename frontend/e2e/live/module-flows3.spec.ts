import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第四轮:路由/空态/筛选持久化/配置分区只读/直达页(部署实例 192.168.6.236)", () => {
  test.slow();

  test("无效路由回退首页不白屏", async ({ page }) => {
    await page.goto("/nonexistent-page-xyz");
    await expect(page.locator(".app-sidebar, nav").first()).toBeVisible({ timeout: 30_000 });
    // 不白屏:body 有实际内容
    const bodyText = await page.locator("body").innerText();
    expect(bodyText.trim().length).toBeGreaterThan(20);
    // 回退到首页视图(发现板块或主导航可用)
    await expect(page.locator("nav").first()).toBeVisible({ timeout: 15_000 });
  });

  test("搜索无结果显示明确空态", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("searchbox", { name: "搜索电影或电视剧" }).fill("zzzzq不存在xyzzy");
    await page.getByRole("button", { name: "提交搜索" }).click();
    await expect(page.getByRole("heading", { name: "搜索结果" })).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText("没有找到相关影视")).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ ...shot("search-empty"), fullPage: true });
  });

  test("目录筛选写回 URL 并刷新保持", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    // 点一个年份按钮(非「全部」)
    const yearBtn = page.locator(".filter-row", { hasText: "年份" }).locator("button").nth(1);
    const yearText = (await yearBtn.textContent())?.trim() ?? "";
    await yearBtn.click();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    expect(page.url()).toContain("year=");
    // 刷新保持
    await page.reload();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    await expect(page.locator(".filter-row", { hasText: "年份" }).locator("button", { hasText: yearText })).toHaveClass(/active/, { timeout: 30_000 });
    await page.screenshot({ ...shot("catalog-year-persist"), fullPage: true });
  });

  test("搜索来源(Prowlarr)配置分区只读浏览", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "搜索来源" }).click();
    await expect(page.getByRole("heading", { name: "Prowlarr" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("#prowlarr-base-url")).toBeVisible({ timeout: 30_000 });
    // 不保存、不改写;仅确认表单存在
    await page.screenshot({ ...shot("settings-prowlarr"), fullPage: true });
  });

  test("日志保留策略设置分区可浏览", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "日志" }).click();
    await expect(page.getByRole("heading", { name: "日志" }).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("日志保留").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("input[min='1'][max='90']").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("settings-logging"), fullPage: true });
  });

  test("详情页检测入口控件存在(不触发)", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(
      page.locator(".resource-table tbody tr, .resource-card, .season-empty, .resource-table .resource-page-error").first(),
    ).toBeVisible({ timeout: 240_000 });
    const inspectBtn = page.locator("button.inspection-more-button").first();
    // 检测入口按钮(开始检测/检测更多)在资源可用时应出现;空态下允许缺失
    if (await page.locator(".resource-table tbody tr, .resource-card").count()) {
      await expect(inspectBtn).toBeVisible({ timeout: 30_000 });
    }
    await page.screenshot({ ...shot("detail-inspect-entry"), fullPage: true });
  });

  test("追更通知分区:渠道列表与添加表单可浏览(不提交)", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "追更通知" }).click();
    await expect(page.getByRole("heading", { name: "追更通知" })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "添加通知渠道" })).toBeVisible({ timeout: 30_000 });
    // 渠道表单控件存在(不填写不提交):名称输入框 + 渠道类型下拉
    await expect(page.locator("label", { hasText: "渠道名称" }).locator("input")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("label", { hasText: "渠道类型" }).locator("select")).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("settings-notify"), fullPage: true });
  });

  test("收藏/记录/工作流直达页渲染", async ({ page }) => {
    for (const [pathname, heading] of [
      ["/favorites", "收藏"],
      ["/history", "记录"],
      ["/workflows", "工作流"],
    ] as Array<[string, string]>) {
      await page.goto(pathname);
      await expect(page.locator("body")).not.toBeEmpty({ timeout: 30_000 });
      await expect(page.locator("nav").first()).toBeVisible({ timeout: 15_000 });
      const bodyText = await page.locator("body").innerText();
      expect(bodyText.trim().length).toBeGreaterThan(20);
    }
    await page.goto("/favorites");
    await expect(page.getByRole("heading", { name: "收藏" })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("direct-pages"), fullPage: true });
  });

  test("移动端:收藏页与通知中心直达无横向溢出", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
    const page = await context.newPage();
    for (const pathname of ["/favorites", "/notifications", "/history"]) {
      await page.goto(`http://192.168.6.236:8115${pathname}`);
      await expect(page.locator("nav").first()).toBeVisible({ timeout: 30_000 });
      await page.waitForTimeout(1500);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      expect(overflow, `${pathname} 横向溢出`).toBe(false);
    }
    await page.screenshot({ ...shot("mobile-direct-pages"), fullPage: true });
  });
});
