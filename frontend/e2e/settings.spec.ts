import { expect, test } from "@playwright/test";
import path from "node:path";

const overview = {
  revision: "rev-1",
  version: "0.4.0",
  uptime_seconds: 90061,
  database_size_bytes: 3145728,
  components: [
    { name: "数据库", status: "ok", detail: "WAL" },
    { name: "PanSou", status: "degraded", detail: "缓存可用" },
    { name: "TMDB", status: "ok", detail: null },
  ],
};
const p115 = {
  enabled: true,
  readiness: "ready",
  cookie_source: "file",
  cookie_structure: "valid",
  cookie_synced_at: "2026-07-25T02:00:00Z",
  capabilities: { magnet: true, share: false },
};
const logItems = [
  { id: "log-1", timestamp: "2026-07-25T02:00:00Z", level: "info", category: "system", message: "服务已启动" },
  { id: "log-2", timestamp: "2026-07-25T02:01:00Z", level: "warning", category: "push", message: "115 分享转存未启用" },
];

test("settings overview, logs, p115 validation, and responsive layout", async ({ page }, testInfo) => {
  let loggingRevision = "rev-1";
  let saveCount = 0;
  let validateCount = 0;
  let logsCount = 0;
  let conflictNextSave = true;

  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-settings" } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({
    json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] },
  }));
  await page.route("**/api/v1/settings/overview", (route) => route.fulfill({ json: overview }));
  await page.route("**/api/v1/settings/logging", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: { revision: loggingRevision, level: "info", retention_days: 30, capacity_mb: 512 } });
    }
    saveCount += 1;
    if (conflictNextSave) {
      conflictNextSave = false;
      return route.fulfill({ status: 409, json: { detail: "revision conflict" } });
    }
    loggingRevision = "rev-2";
    return route.fulfill({ json: { revision: loggingRevision, level: "warning", retention_days: 30, capacity_mb: 512 } });
  });
  await page.route("**/api/v1/settings/p115/validate", (route) => {
    validateCount += 1;
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({});
    return route.fulfill({ json: p115 });
  });
  await page.route("**/api/v1/settings/p115", (route) => route.fulfill({ json: p115 }));
  await page.route("**/api/v1/logs?**", (route) => {
    logsCount += 1;
    const params = new URL(route.request().url()).searchParams;
    return route.fulfill({
      json: {
        items: logItems,
        page: Number(params.get("page") ?? "1"),
        page_size: Number(params.get("page_size") ?? "20"),
        total: 2,
        total_pages: 1,
      },
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await expect(page.getByText("0.4.0")).toBeVisible();
  await expect(page.getByText("PanSou")).toBeVisible();

  if (testInfo.project.name === "mobile") {
    await page.locator(".settings-mobile-select select").selectOption("logs");
  } else {
    await page.getByRole("button", { name: "日志" }).click();
  }
  const logMessage = testInfo.project.name === "mobile"
    ? page.locator(".settings-log-item p").filter({ hasText: "服务已启动" })
    : page.locator(".settings-log-table td").filter({ hasText: "服务已启动" });
  await expect(logMessage).toBeVisible();
  await expect.poll(() => logsCount).toBe(1);
  await page.locator(".settings-filter-row select").first().selectOption("error");
  await expect.poll(() => logsCount).toBe(2);
  await page.getByLabel("最低级别").selectOption("warning");
  await expect(page.getByText("有未保存的日志设置")).toBeVisible();
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toBeVisible();
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toHaveCount(0);
  await page.getByLabel("最低级别").selectOption("warning");
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => saveCount).toBe(2);

  if (testInfo.project.name === "mobile") {
    await page.locator(".settings-mobile-select select").selectOption("p115");
  } else {
    await page.getByRole("button", { name: "115 推送" }).click();
  }
  await expect(page.getByText("连接状态")).toBeVisible();
  await expect(page.getByText("115 分享转存")).toBeVisible();
  await expect(page.getByText("未启用")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("Cookie 验证完成")).toBeVisible();
  await expect.poll(() => validateCount).toBe(1);
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  await page.evaluate(() => window.scrollTo(0, 0));
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    content: document.querySelector(".settings-content")?.getBoundingClientRect().right ?? 0,
  }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.content).toBeLessThanOrEqual(layout.clientWidth + 1);
  await page.screenshot({ path: path.resolve("e2e-screenshots", `settings-${testInfo.project.name}.png`), fullPage: true });
});
