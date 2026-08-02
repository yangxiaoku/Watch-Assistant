import { expect, test } from "@playwright/test";

async function installCoreRoutes(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-core" } }));
  await page.route("**/api/v1/workflows?**", (route) => route.fulfill({ json: { items: [], page: 1, page_size: 20, total: 0 } }));
  await page.route("**/api/v1/notifications?**", (route) => route.fulfill({ json: { items: [], unread_count: 0 } }));
  await page.route("**/api/v1/notification-preferences", (route) => route.fulfill({ json: { enabled: true, muted_event_codes: [], quiet_hours_enabled: false, quiet_hours_start: "23:00", quiet_hours_end: "08:00", quiet_hours_timezone: "Asia/Shanghai", error_bypass_quiet_hours: true, revision: 1 } }));
}

test("keeps core workflow destinations visible and inside the viewport", async ({ page }, testInfo) => {
  await installCoreRoutes(page);
  await page.goto("/workflows");
  await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible();

  for (const name of ["推送任务", "任务中心", "通知", "设置"]) {
    const button = page.getByRole("button", { name, exact: true });
    await expect(button).toBeVisible();
    const box = await button.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(testInfo.project.use.viewport!.width!);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await page.getByRole("button", { name: "推送任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "推送任务" })).toBeVisible();
  await page.getByRole("button", { name: "任务中心", exact: true }).last().click();
  await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible();
  await page.getByRole("button", { name: "查看推送任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "推送任务" })).toBeVisible();
  await page.getByRole("button", { name: "关闭推送任务", exact: true }).click();

  await page.getByRole("button", { name: "通知", exact: true }).click();
  await expect(page.getByRole("heading", { name: /通知中心/ })).toBeVisible();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  if (testInfo.project.name === "mobile" || testInfo.project.name === "desktop") {
    await page.screenshot({ path: testInfo.outputPath(`core-workflow-navigation-${testInfo.project.name}.png`), fullPage: true });
  }
});
