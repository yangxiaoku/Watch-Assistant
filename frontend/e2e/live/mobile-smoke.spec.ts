import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");

test.describe("移动端冒烟(真实用户路径)", () => {
  test.slow();

  test("mobile: 首页无横向溢出", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".auth-gate")).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByText("正在热映", { exact: true }).first()).toBeVisible({ timeout: 30_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.screenshot({ path: path.join(SHOTS, "m01-home-mobile.png") });
  });

  test("mobile: 目录→详情完整路径", async ({ page }) => {
    await page.goto("/movies");
    const card = page.locator(".movie-card").first();
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.click();
    await expect(page).toHaveURL(/\/movie\/\d+/);
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 30_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.screenshot({ path: path.join(SHOTS, "m02-detail-mobile.png") });
  });
});
