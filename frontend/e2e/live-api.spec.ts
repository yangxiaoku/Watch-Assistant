import { expect, test } from "@playwright/test";

test("built frontend uses the live authenticated inspection API", async ({ page }, testInfo) => {
  await page.goto("/movie/12345");
  await page.getByLabel("Web 密码").fill("watch-assistant-live-test");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("heading", { name: "Live Test Film", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "检测本页磁力" })).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("1 / 1");
  await expect(page.getByRole("status")).toContainText("检测完成");
  await expect(page.locator(".resource-table tbody tr")).toContainText("1.0 GB");
  await expect(page.locator(".resource-table tbody tr")).toContainText("已检测");
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("live-api.png"), fullPage: true });
});

test("runs full STRM generation after the live complete library scan", async ({ page }, testInfo) => {
  await page.goto("/library");
  await page.getByLabel("Web 密码").fill("watch-assistant-live-test");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("heading", { name: "媒体库与 STRM" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "本地 STRM 验收媒体库" })).toBeVisible();

  await page.getByRole("button", { name: "扫描目录" }).click();
  await expect(page.getByText("扫描完成，共发现 2 项")).toBeVisible();
  await expect(page.getByText("扫描状态").locator(".." )).toContainText("完整");
  await expect(page.getByRole("cell", { name: "Episode.mkv", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "全量 STRM" }).click();
  await expect(page.getByText(/STRM 全量同步完成/)).toBeVisible();
  await expect(page.getByText("全量生成").last()).toBeVisible();
  await expect(page.getByText("已完成").last()).toBeVisible();
  await expect(page.getByText("Shows/Episode.strm")).toBeVisible();
  await expect(page.locator(".library-output-section").filter({ hasText: "STRM 文件" })).toContainText("Shows/Episode.strm");
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`live-strm-${testInfo.project.name}.png`), fullPage: true });
});
