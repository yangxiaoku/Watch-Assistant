import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("订阅/季度搜索/自动清理 上线功能(部署实例 192.168.6.236)", () => {
  test.slow(); // 远程部署 + TMDB/PanSou 依赖,放宽超时

  test("详情页订阅按钮:创建→已订阅→跳转订阅管理→取消清理", async ({ page }) => {
    // 预读当前订阅列表,挑一部未订阅的电影
    const existing = await page.request.get("/api/v1/subscriptions");
    const subs = (await existing.json()) as Array<{ tmdb_id: number; media_type: string; season_number: number | null; status: string }>;
    // 只把「非取消/非完成」的订阅视为已订阅(已取消的仍可重新订阅)
    const subscribedKeys = new Set(
      subs.filter((s) => s.status !== "cancelled" && s.status !== "completed").map((s) => `${s.media_type}:${s.tmdb_id}:${s.season_number ?? ""}`),
    );

    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 45_000 });

    let detailUrl = "";
    const cards = page.locator(".movie-card");
    await expect(cards.first()).toBeVisible({ timeout: 45_000 });
    // 等目录加载遮罩消失,避免点击落到遮罩上
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    for (let i = 0; i < Math.min(await cards.count(), 8); i++) {
      await cards.nth(i).click();
      await page.waitForURL(/\/movie\/\d+/, { timeout: 60_000 });
      detailUrl = page.url();
      const tmdbId = Number(detailUrl.match(/\/movie\/(\d+)/)?.[1]);
      if (!subscribedKeys.has(`movie:${tmdbId}:`)) break;
      await page.goBack();
      await page.waitForURL(/\/movies/, { timeout: 30_000 });
    }
    const tmdbId = Number(detailUrl.match(/\/movie\/(\d+)/)?.[1]);
    expect(tmdbId).toBeGreaterThan(0);

    const subscribeBtn = page.locator("button.subscribe-inline");
    await expect(subscribeBtn).toBeVisible({ timeout: 45_000 });
    if ((await subscribeBtn.textContent())?.trim() === "订阅") {
      await subscribeBtn.click();
      // 并发容错:与其他订阅测试并行时可能因创建冲突/竞态未即时生效,重试一次
      await expect(subscribeBtn).toHaveText(/已订阅|已暂停/, { timeout: 15_000 }).catch(async () => {
        if ((await subscribeBtn.textContent())?.trim() === "订阅") {
          await subscribeBtn.click();
          await expect(subscribeBtn).toHaveText(/已订阅|已暂停/, { timeout: 15_000 });
        }
      });
      await page.screenshot({ ...shot("detail-subscribed"), fullPage: true });
    }

    // 已订阅 → 跳转订阅管理
    await subscribeBtn.click();
    await expect(page).toHaveURL(/\/subscriptions/, { timeout: 15_000 });
    await expect(page.getByText("订阅关注的影视，自动检查新资源并推送通知。")).toBeVisible({ timeout: 15_000 });

    // 定位本次创建的订阅卡片并取消清理(上线功能验收:取消按钮真实生效)
    const card = page.locator(".subscription-card", { hasText: `TMDB #${tmdbId}` }).first();
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.locator('button[title="取消订阅"]').click();
    await expect(card.locator('.status-tag[data-status="cancelled"]')).toBeVisible({ timeout: 15_000 });
    await page.screenshot({ ...shot("subscription-cancelled"), fullPage: true });
  });

  test("剧集季度切换:不出现误导错误,空态或资源二选一", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("searchbox", { name: "搜索电影或电视剧" }).fill("权力的游戏");
    await page.getByRole("button", { name: "提交搜索" }).click();
    await expect(page.getByRole("heading", { name: "搜索结果" })).toBeVisible({ timeout: 45_000 });
    const card = page.locator(".movie-card").first();
    await expect(card).toBeVisible({ timeout: 45_000 });
    await card.click();
    await expect(page).toHaveURL(/\/tv\/\d+/, { timeout: 60_000 });

    const seasonSelect = page.locator("#season-select");
    await expect(seasonSelect).toBeVisible({ timeout: 45_000 });
    const seasonValues = (await seasonSelect.locator("option").evaluateAll((opts) =>
      opts.map((o) => (o as HTMLOptionElement).value).filter((v) => v !== ""),
    )).slice(0, 3);

    for (const value of seasonValues) {
      await seasonSelect.selectOption(value);
      // 等待该季度搜索完成(真实上游搜索,预算 180s,放宽到 240s)
      await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
      // 资源区稳定后:空态 / 资源行(或卡片) / 错误提示 三选一出现
      await expect(
        page.locator(".season-empty, .resource-table tbody tr, .resource-card, .resource-table .resource-page-error").first(),
      ).toBeVisible({ timeout: 30_000 });
      // 切换季度不得出现误导性分页错误
      await expect(page.getByText("资源分页暂不可用")).toHaveCount(0);
      // 空态与错误不得并存
      const hintCount = await page.locator(".season-empty").count();
      const errorCount = await page.locator(".resource-table .resource-page-error").count();
      expect(hintCount + errorCount).toBeLessThanOrEqual(1);
    }
    await page.screenshot({ ...shot("tv-season-switch"), fullPage: true });
  });

  test("设置:自动清理广告垃圾文件开关持久化并还原", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    // 设置分区导航 → 115 整理
    await page.locator(".settings-nav button", { hasText: "115 整理" }).click();
    await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible({ timeout: 30_000 });
    // 自动清理开关位于「高级设置」折叠区内,先展开
    await page.locator("details.settings-subsection summary", { hasText: "高级设置" }).click();

    const toggle = page
      .locator("label.settings-toggle", { hasText: "自动清理广告垃圾文件" })
      .locator("input[type=checkbox]");
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    const initial = (await toggle.isChecked()) as boolean;

    // 翻转并保存
    await toggle.setChecked(!initial);
    await page.locator(".settings-save-bar").getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("整理设置已保存")).toBeVisible({ timeout: 15_000 });

    // 刷新后保持
    await page.reload();
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "115 整理" }).click();
    await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible({ timeout: 30_000 });
    await page.locator("details.settings-subsection summary", { hasText: "高级设置" }).click();
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    expect(await toggle.isChecked()).toBe(!initial);

    // 还原默认值(生产保持默认关闭)
    await toggle.setChecked(initial);
    await page.locator(".settings-save-bar").getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("整理设置已保存")).toBeVisible({ timeout: 15_000 });
    await page.reload();
    await expect(page.locator(".settings-nav")).toBeVisible({ timeout: 30_000 });
    await page.locator(".settings-nav button", { hasText: "115 整理" }).click();
    await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible({ timeout: 30_000 });
    await page.locator("details.settings-subsection summary", { hasText: "高级设置" }).click();
    await expect(toggle).toBeVisible({ timeout: 30_000 });
    expect(await toggle.isChecked()).toBe(initial);
    await page.screenshot({ ...shot("settings-junk-toggle"), fullPage: true });
  });
});
