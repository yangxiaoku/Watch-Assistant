import { expect, test } from "@playwright/test";

const overview = {
  release: "2026.07.25",
  uptime_seconds: 90061,
  database_size_bytes: 3145728,
  capabilities: { inspection: true, magnet: true, share: false },
};
const p115 = {
  enabled: true,
  ready: true,
  capabilities: { magnet: true, share: false },
  cookie: {
    source: "tgtodrive",
    configured: true,
    structure_valid: true,
    sync_status: "success",
    last_sync_at: "2026-07-25T02:00:00Z",
  },
  target_configured: true,
  max_concurrency: 1,
};

test("settings contract, cursor logs, validation states, and responsive layout", async ({ page }, testInfo) => {
  let loggingRevision = 0;
  let saveCount = 0;
  let validateCount = 0;
  let logsCount = 0;
  let conflictNextSave = true;
  const patchBodies: unknown[] = [];
  const validationStatuses = ["ready", "needs_auth", "unavailable"] as const;

  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-settings" } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] } }));
  await page.route("**/api/v1/settings/overview", (route) => route.fulfill({ json: overview }));
  await page.route("**/api/v1/settings/logging", (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: { revision: loggingRevision, level: "INFO", retention_days: 30, max_file_mb: 10 } });
    expect(route.request().method()).toBe("PATCH");
    patchBodies.push(route.request().postDataJSON());
    saveCount += 1;
    if (conflictNextSave) {
      conflictNextSave = false;
      return route.fulfill({ status: 409, json: { detail: "revision conflict" } });
    }
    loggingRevision = 1;
    return route.fulfill({ json: { revision: loggingRevision, level: "WARNING", retention_days: 45, max_file_mb: 20 } });
  });
  await page.route("**/api/v1/settings/p115/validate", (route) => {
    const status = validationStatuses[validateCount] ?? "unavailable";
    validateCount += 1;
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({});
    return route.fulfill({ json: { status, checked_at: "2026-07-25T02:00:00Z" } });
  });
  await page.route("**/api/v1/settings/p115", (route) => route.fulfill({ json: p115 }));
  await page.route("**/api/v1/logs?**", (route) => {
    logsCount += 1;
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("limit")).toBe("20");
    expect(params.get("level")).toBeNull();
    if (params.get("category") === "search") return route.fulfill({ json: { items: [{ id: 9, timestamp: "2026-07-25T02:03:00Z", level: "INFO", category: "search", message: "新分类响应" }], next_cursor: null } });
    if (params.get("cursor") === "20") return route.fulfill({ json: { items: [{ id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "缓存已刷新（重复）" }, { id: 3, timestamp: "2026-07-25T02:02:00Z", level: "ERROR", category: "security", message: "需要重新登录" }], next_cursor: null } });
    expect(params.get("cursor")).toBeNull();
    return route.fulfill({ json: { items: [{ id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO", category: "system", message: "服务已启动" }, { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "缓存已刷新" }], next_cursor: 20 } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await expect(page.getByText("2026.07.25")).toBeVisible();
  await expect(page.getByText("内容检测")).toBeVisible();

  if (testInfo.project.name === "mobile") await page.locator(".settings-mobile-select select").selectOption("logs");
  else await page.getByRole("button", { name: "日志" }).click();
  const logMessage = testInfo.project.name === "mobile" ? page.locator(".settings-log-item p").filter({ hasText: "服务已启动" }) : page.locator(".settings-log-table td").filter({ hasText: "服务已启动" });
  await expect(logMessage).toBeVisible();
  await expect.poll(() => logsCount).toBe(1);
  await page.getByRole("button", { name: "加载更多" }).click();
  const laterMessage = testInfo.project.name === "mobile" ? page.locator(".settings-log-item p").filter({ hasText: "需要重新登录" }) : page.locator(".settings-log-table td").filter({ hasText: "需要重新登录" });
  await expect(laterMessage).toBeVisible();
  await expect(page.getByText("已加载 3 条")).toBeVisible();
  await page.locator(".settings-filter-row select").selectOption("search");
  const categoryMessage = testInfo.project.name === "mobile" ? page.locator(".settings-log-item p").filter({ hasText: "新分类响应" }) : page.locator(".settings-log-table td").filter({ hasText: "新分类响应" });
  await expect(categoryMessage).toBeVisible();
  await expect(page.getByText("服务已启动")).toHaveCount(0);
  await expect.poll(() => logsCount).toBe(3);

  await page.getByLabel("最低级别").selectOption("WARNING");
  await page.getByLabel("保留天数（1-90）").fill("45");
  await page.getByLabel("文件上限（MB，1-50）").fill("20");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toBeVisible();
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toHaveCount(0);
  await page.getByLabel("最低级别").selectOption("WARNING");
  await page.getByLabel("保留天数（1-90）").fill("45");
  await page.getByLabel("文件上限（MB，1-50）").fill("20");
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => saveCount).toBe(2);
  expect(patchBodies[0]).toEqual({ revision: 0, level: "WARNING", retention_days: 45, max_file_mb: 20 });

  if (testInfo.project.name === "mobile") await page.locator(".settings-mobile-select select").selectOption("p115");
  else await page.getByRole("button", { name: "115 推送" }).click();
  await expect(page.getByText("已就绪")).toBeVisible();
  await expect(page.getByText("TgtoDrive")).toBeVisible();
  await expect(page.getByText("结构正常")).toBeVisible();
  await expect(page.getByText("115 分享转存")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("Cookie 已就绪")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("Cookie 需要重新授权")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("115 当前不可用")).toBeVisible();
  await expect(page.getByText("结构正常")).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  await page.evaluate(() => window.scrollTo(0, 0));
  const layout = await page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth, contentRight: document.querySelector(".settings-content")?.getBoundingClientRect().right ?? 0 }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.contentRight).toBeLessThanOrEqual(layout.clientWidth + 1);
  await page.screenshot({ path: testInfo.outputPath(`settings-${testInfo.project.name}.png`), fullPage: true });
});
