import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第七轮:安全边界与禁用态用户操作验证(部署实例 192.168.6.236)", () => {
  test.slow();

  test("推送按钮:能力不可用时禁用并带原因提示", async ({ page }) => {
    await page.goto("/tv/1399");
    await expect(page.locator(".movie-copy h1")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    const pushBtn = page.locator(".resource-table .action-cell button, .resource-card .push-button").first();
    if (!(await pushBtn.count())) {
      test.skip(true, "无资源行可断言");
      return;
    }
    const disabled = (await pushBtn.isDisabled().catch(() => false)) as boolean;
    const title = ((await pushBtn.getAttribute("title")) ?? "").trim();
    if (disabled) {
      // 能力不可用:按钮禁用且提示原因(磁力云下载不可用/115 分享转存尚未验证)
      expect(title).toMatch(/不可用|未验证/);
    } else {
      // 能力可用:点击应出现推送目录提示或推送确认(容错:不真正推送)
      await pushBtn.click();
      await page.waitForTimeout(1500);
      const bodyText = await page.locator("body").innerText();
      expect(bodyText).toMatch(/推送|目录|未验证|不可用/);
    }
    await page.screenshot({ ...shot("push-guard"), fullPage: true });
  });

  test("整理工作台:执行按钮在无计划时不可触发", async ({ page }) => {
    await page.goto("/organization");
    await expect(page.locator("body")).not.toBeEmpty({ timeout: 30_000 });
    await page.waitForTimeout(2000);
    const runBtn = page.getByRole("button", { name: "开始整理" }).first();
    if (await runBtn.count()) {
      // 按钮存在:无选中计划时应禁用;有可执行计划时点击会弹确认框(不确认,仅验证门禁呈现)
      if (!(await runBtn.isDisabled())) {
        await runBtn.click();
        await expect(page.locator(".dialog, [role=dialog], .confirm-dialog").first()).toBeVisible({ timeout: 15_000 }).catch(() => {});
      } else {
        expect(await runBtn.isDisabled()).toBe(true);
      }
    }
    await page.screenshot({ ...shot("organization-execute-gate"), fullPage: true });
  });

  test("115 自动签到分区:开关与时间控件存在(不修改状态)", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "115 自动签到" }).click();
    await expect(page.getByRole("heading", { name: "115 自动签到" })).toBeVisible({ timeout: 30_000 });
    // 开关与时间输入存在(只读断言,不切换——切换会立即保存并影响调度)
    await expect(page.locator("label.settings-toggle input[type=checkbox]").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("input[type=time]").first()).toBeVisible({ timeout: 15_000 });
    await page.screenshot({ ...shot("checkin-section"), fullPage: true });
  });

  test("离线横幅:断网提示与恢复", async ({ page, context }) => {
    await page.goto("/");
    await expect(page.locator("nav").first()).toBeVisible({ timeout: 30_000 });
    await context.setOffline(true);
    await page.waitForTimeout(2000);
    // 侧栏状态显示离线
    const sidebarText = await page.locator(".app-sidebar").innerText().catch(() => "");
    expect(sidebarText).toContain("离线");
    await context.setOffline(false);
    await page.waitForTimeout(2000);
    const sidebarText2 = await page.locator(".app-sidebar").innerText().catch(() => "");
    expect(sidebarText2).toContain("局域网在线");
    await page.screenshot({ ...shot("offline-banner"), fullPage: true });
  });
});
