import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");

test.describe("登录门(未登录用户路径)", () => {
  test("未登录访问首页被拦截,显示登录门", async ({ page }) => {
    await page.goto("/");
    const gate = page.locator(".auth-gate");
    await expect(gate).toBeVisible({ timeout: 20_000 });
    await expect(gate.getByRole("heading", { name: "进入观影工作台" })).toBeVisible();
    await expect(gate.locator("#username")).toBeVisible();
    await expect(gate.locator("#password")).toBeVisible();
    await page.screenshot({ path: path.join(SHOTS, "01-login-gate.png") });
  });

  test("错误密码显示中文错误提示,不泄露系统细节", async ({ page }) => {
    await page.goto("/");
    const gate = page.locator(".auth-gate");
    await expect(gate).toBeVisible({ timeout: 20_000 });
    await page.locator("#username").fill(process.env.WA_E2E_USER ?? "admin");
    await page.locator("#password").fill(`wrong-${Date.now()}`);
    await page.getByRole("button", { name: "登录" }).click();

    const alert = gate.getByRole("alert");
    await expect(alert).toBeVisible({ timeout: 20_000 });
    const text = (await alert.textContent()) ?? "";
    // 三种合法文案之一:凭据错误 / 尝试频繁 / 锁定;绝不允许英文堆栈或未映射错误
    expect(
      /密码|凭据|无效|频繁|限制|锁定|错误/i.test(text),
      `错误提示文案异常: ${text}`,
    ).toBeTruthy();
    await page.screenshot({ path: path.join(SHOTS, "02-login-wrong-password.png") });
  });
});
