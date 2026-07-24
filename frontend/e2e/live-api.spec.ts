import { expect, test } from "@playwright/test";

test("built frontend uses the live authenticated inspection API", async ({ page }, testInfo) => {
  await page.goto("/movie/12345");
  await page.getByLabel("Web 密码").fill("watch-assistant-live-test");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("heading", { name: "Live Test Film", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "检测本页磁力" })).toBeVisible();
  await page.getByRole("button", { name: "检测本页磁力" }).click();
  await expect(page.getByRole("status")).toContainText("1 / 1");
  await expect(page.getByRole("status")).toContainText("检测完成");
  await expect(page.locator(".resource-table tbody tr")).toContainText("1.0 GB");
  await expect(page.locator(".resource-table tbody tr")).toContainText("已检测");
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("live-api.png"), fullPage: true });
});
