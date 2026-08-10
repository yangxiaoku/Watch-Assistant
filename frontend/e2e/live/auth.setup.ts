import { test as setup, expect } from "@playwright/test";

/**
 * 通过真实浏览器登录门登录,保存会话供后续用例复用。
 * 凭据只来自环境变量,禁止写入源码/日志/截图。
 */
const username = process.env.WA_E2E_USER;
const password = process.env.WA_E2E_PASSWORD;

setup("浏览器登录并保存会话", async ({ page }) => {
  expect(username, "缺少 WA_E2E_USER 环境变量").toBeTruthy();
  expect(password, "缺少 WA_E2E_PASSWORD 环境变量").toBeTruthy();

  await page.goto("/");
  const gate = page.locator(".auth-gate");
  await expect(gate).toBeVisible({ timeout: 20_000 });

  await page.locator("#username").fill(username as string);
  await page.locator("#password").fill(password as string);
  await page.getByRole("button", { name: "登录" }).click();

  // 登录成功:登录门消失,主导航出现
  await expect(gate).toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();

  await page.context().storageState({ path: "test-results/deploy/state.json" });
});
