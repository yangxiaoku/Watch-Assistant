import { expect, test } from "@playwright/test";

const movie = {
  tmdb_id: 27205,
  media_type: "movie" as const,
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "梦境中的梦境。",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [],
  vote_average: 8.4,
};

test("connects Chinese workbench entries without horizontal overflow", async ({ page }, testInfo) => {
  await page.route("**/api/v1/health", (route) => route.fulfill({
    json: {
      status: "ok",
      push_supported: false,
      inspection_supported: false,
      organization_plan_enabled: true,
      organization_execution_enabled: false,
      organization_execution_supported: false,
      strm_capabilities: { full: true, incremental: true, cleanup: false, playback: false, playback_contract_verified: false },
    },
  }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] } }));
  await page.route("**/api/v1/media/search?**", (route) => route.fulfill({
    json: { results: [movie], page: 1, total_pages: 1, total_results: 1 },
  }));

  const isMobile = testInfo.project.name.startsWith("mobile");
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "主导航" });
  if (isMobile) {
    await page.getByRole("button", { name: "菜单", exact: true }).click();
    await expect(page.locator(".app-sidebar")).toHaveClass(/mobile-open/);
    await expect(nav.getByRole("button", { name: "首页", exact: true })).toBeInViewport();
  }
  for (const label of ["首页", "电影", "剧集", "热门", "收藏", "记录", "整理", "媒体库"]) {
    await expect(nav.getByRole("button", { name: label, exact: true })).toBeVisible();
  }
  const navLayout = await page.evaluate(() => {
    const navEl = document.querySelector<HTMLElement>(".app-sidebar .sidebar-nav");
    if (!navEl) throw new Error("missing sidebar navigation");
    return {
      clientWidth: navEl.clientWidth,
      scrollWidth: navEl.scrollWidth,
      buttons: Array.from(navEl.querySelectorAll<HTMLElement>("button")).map((button) => {
        const box = button.getBoundingClientRect();
        return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
      }),
    };
  });
  expect(navLayout.scrollWidth).toBeLessThanOrEqual(navLayout.clientWidth);
  for (const button of navLayout.buttons) {
    expect(button.left).toBeGreaterThanOrEqual(-1);
    expect(button.right).toBeLessThanOrEqual((testInfo.project.use.viewport?.width ?? 0) + 1);
  }
  // 侧栏为纵向布局：每个入口一行
  const navRows = new Set(navLayout.buttons.map((button) => Math.round(button.top)));
  expect(navRows.size).toBeGreaterThanOrEqual(6);

  if (isMobile) {
    await page.getByRole("button", { name: "菜单", exact: true }).click();
  }
  await page.getByLabel("搜索电影或电视剧").fill("沙丘");
  await page.getByLabel("搜索电影或电视剧").press("Enter");
  await expect(page.getByRole("heading", { name: "“沙丘”的搜索结果" })).toBeVisible();
  await expect(page.getByRole("button", { name: "首页", exact: true })).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  if (testInfo.project.name === "desktop" || testInfo.project.name === "mobile") {
    await page.screenshot({ path: testInfo.outputPath(`workbench-${testInfo.project.name}.png`), fullPage: true });
  }
});
