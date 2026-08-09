import { expect, test } from "@playwright/test";

test("logs page cursor pagination, filters, and status visibility", async ({ page }, testInfo) => {
  const mobileLayout = testInfo.project.name.startsWith("mobile");
  let logsCount = 0;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-logs" } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] } }));
  await page.route("**/api/v1/logs?**", (route) => {
    logsCount += 1;
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("limit")).toBe("20");
    expect(params.get("level")).toBeNull();
    if (params.get("category") === "search") {
      return route.fulfill({ json: { items: [{ id: 9, timestamp: "2026-07-25T02:03:00Z", level: "INFO", category: "search", message: "search.new_category", message_zh: "新分类响应" }], next_cursor: null } });
    }
    if (params.get("cursor") === "20") {
      return route.fulfill({ json: { items: [{ id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "cache.refreshed", message_zh: "缓存已刷新（重复）" }, { id: 3, timestamp: "2026-07-25T02:02:00Z", level: "ERROR", category: "security", message: "auth.relogin_required", message_zh: "需要重新登录" }], next_cursor: null } });
    }
    expect(params.get("cursor")).toBeNull();
    return route.fulfill({ json: { items: [{ id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO", category: "system", message: "system.started", message_zh: "服务已启动" }, { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "cache.refreshed", message_zh: "缓存已刷新" }], next_cursor: 20 } });
  });

  await page.goto("/logs");
  await expect(page.getByRole("heading", { name: "日志" })).toBeVisible();
  const logMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "服务已启动" }) : page.locator(".settings-log-table td").filter({ hasText: "服务已启动" });
  await expect(logMessage).toBeVisible();
  await expect.poll(() => logsCount).toBe(1);

  await page.getByRole("button", { name: "加载更多" }).click();
  const laterMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "需要重新登录" }) : page.locator(".settings-log-table td").filter({ hasText: "需要重新登录" });
  await expect(laterMessage).toBeVisible();
  await expect(page.getByText("已加载 3 条")).toBeVisible();

  await page.locator(".settings-filter-row select").first().selectOption("search");
  const categoryMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "新分类响应" }) : page.locator(".settings-log-table td").filter({ hasText: "新分类响应" });
  await expect(categoryMessage).toBeVisible();
  await expect(page.getByText("服务已启动")).toHaveCount(0);
  await expect.poll(() => logsCount).toBe(3);
});
