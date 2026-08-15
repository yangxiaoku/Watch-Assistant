import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("真实用户核心流程(部署实例 192.168.6.236)", () => {
  test.slow(); // 远程部署 + TMDB 依赖,放宽超时

  test("首页:发现板块完整加载", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("button", { name: "首页" })).toHaveClass(/active/);
    for (const section of ["正在热映", "本周热门", "热播剧集", "即将上映", "高分佳片", "高分剧集"]) {
      await expect(page.getByText(section, { exact: true }).first()).toBeVisible({ timeout: 30_000 });
    }
    await page.screenshot({ ...shot("03-home"), fullPage: true });
  });

  test("电影目录:浏览与翻页", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 30_000 });
    const firstCard = page.locator(".movie-card-copy strong").first();
    await expect(firstCard).toBeVisible({ timeout: 30_000 });
    const firstTitle = (await firstCard.textContent())?.trim();
    await page.getByRole("button", { name: "下一页" }).click();
    await expect(page).toHaveURL(/page=2/, { timeout: 30_000 });
    // 等待翻页加载完成(catalog-loading 出现并消失)
    await expect(page.locator(".catalog-loading").first()).toBeVisible({ timeout: 30_000 }).catch(() => {});
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 30_000 });
    const secondTitle = (await page.locator(".movie-card-copy strong").first().textContent())?.trim();
    expect(secondTitle).not.toBe(firstTitle); // 翻页确实换了内容
    await page.screenshot({ ...shot("04-movies-catalog-p2"), fullPage: true });
  });

  test("剧集目录:正常加载", async ({ page }) => {
    await page.goto("/tv");
    await expect(page.getByRole("heading", { name: "剧集库" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("05-tv-catalog"), fullPage: true });
  });

  test("热门页:正常加载", async ({ page }) => {
    await page.goto("/popular");
    await expect(page.getByRole("heading", { name: "热门" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 30_000 });
  });

  test("搜索→详情:顶栏搜索进入影片详情", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("searchbox", { name: "搜索电影或电视剧" }).fill("沙丘");
    await page.getByRole("button", { name: "提交搜索" }).click();
    await expect(page.getByRole("heading", { name: "搜索结果" })).toBeVisible({ timeout: 30_000 });
    const card = page.locator(".movie-card").first();
    await expect(card).toBeVisible({ timeout: 30_000 });
    const movieTitle = (await card.locator(".movie-card-copy strong").textContent())?.trim();
    await card.click();
    await expect(page).toHaveURL(/\/movie\/\d+/);
    const detailTitle = page.locator(".movie-copy h1").first();
    await expect(detailTitle).toBeVisible({ timeout: 30_000 });
    expect((await detailTitle.textContent())?.trim()).toContain((movieTitle ?? "").slice(0, 2));
    await page.screenshot({ ...shot("06-movie-detail"), fullPage: false });
  });

  test("详情页:资源区加载并可筛选质量", async ({ page }) => {
    // 直接打开详情(已知 TMDB 电影),验证资源聚合区
    await page.goto("/movie/438631"); // 沙丘
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-copy h1")).toContainText("沙丘");
    // 资源表头部
    const resourceHeading = page.getByText(/资源/, { exact: false }).first();
    await expect(resourceHeading).toBeVisible({ timeout: 30_000 });
    // 质量筛选 tab(若有)
    const qualityTab = page.getByRole("button", { name: "1080p" }).first();
    if (await qualityTab.isVisible().catch(() => false)) {
      await qualityTab.click();
      await expect(qualityTab).toHaveClass(/active/);
    }
    await page.screenshot({ ...shot("07-detail-resources"), fullPage: false });
  });

  test("整理工作台:状态/待处理/历史三个视图可浏览", async ({ page }) => {
    await page.goto("/organization");
    await expect(page.getByRole("heading", { name: "整理" })).toBeVisible({ timeout: 30_000 });
    const tabs = page.getByRole("tablist", { name: "整理视图" });
    await expect(tabs).toBeVisible({ timeout: 30_000 });
    // 状态视图:自动整理设置卡片或明确空态/错误
    await expect(page.locator(".organization-auto-settings, .organization-empty").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("08-organization-status"), fullPage: true });
    // 待处理计划
    await page.locator(".organization-tabs button", { hasText: "待处理" }).click();
    await expect(page.getByRole("heading", { name: "待处理计划" }).first()).toBeVisible({ timeout: 30_000 });
    // 整理历史
    await page.locator(".organization-tabs button", { hasText: "历史" }).click();
    await expect(page.getByRole("heading", { name: "整理历史" }).first()).toBeVisible({ timeout: 30_000 });
  });

  test("媒体库工作台:可浏览", async ({ page }) => {
    await page.goto("/library");
    await expect(page.getByRole("heading", { name: "媒体库", exact: true }).first()).toBeVisible({ timeout: 30_000 });
    // 库列表/空状态/配置表单/错误条四态其一
    const any = page.locator(".library-scope-row, .library-muted, .library-config-form, .error-strip").first();
    await expect(any).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("09-library"), fullPage: true });
  });

  test("任务中心:列表加载,推送任务抽屉可开合", async ({ page }) => {
    await page.goto("/workflows");
    await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("10-workflows"), fullPage: true });
    // 打开推送任务抽屉再关闭(真实用户操作,不提交任何任务)
    await page.getByRole("button", { name: "推送任务", exact: true }).first().click();
    const drawer = page.locator(".task-drawer, [class*='drawer']");
    await expect(drawer.first()).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: /关闭/ }).first().click();
    await expect(drawer.first()).toHaveCount(0);
  });

  test("通知中心:可浏览", async ({ page }) => {
    await page.goto("/notifications");
    await expect(page.getByRole("heading", { name: "通知" }).or(page.getByRole("heading", { name: "通知中心" })).first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("11-notifications"), fullPage: true });
  });

  test("日志页:可浏览", async ({ page }) => {
    await page.goto("/logs");
    await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 30_000 });
    const any = page.locator(".settings-log-table, .settings-log-item, .settings-empty-block, .settings-state").first();
    await expect(any).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("12-logs"), fullPage: true });
  });

  test("设置中心:全部分区可加载", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "设置" })).toBeVisible({ timeout: 30_000 });
    const sections = ["概览", "日志", "内容安全", "资源检测", "连接配置", "搜索来源", "115 整理"];
    for (const label of sections) {
      const tab = page.locator(".settings-nav button", { hasText: label }).first();
      await tab.click();
      await expect(tab).toHaveClass(/active/, { timeout: 20_000 });
      await expect(page.locator(".settings-section-heading, .settings-content, [class*='settings-section']").first()).toBeVisible({ timeout: 20_000 });
    }
    await page.screenshot({ ...shot("13-settings-overview"), fullPage: true });
  });

  test("收藏与记录页:正常加载", async ({ page }) => {
    for (const [url, heading] of [["/favorites", "收藏"], ["/history", "记录"]] as const) {
      await page.goto(url);
      await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible({ timeout: 30_000 });
    }
  });
});
