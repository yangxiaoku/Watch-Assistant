import { test, expect } from "@playwright/test";
import path from "node:path";

const SHOTS = path.resolve("test-results/deploy/screenshots");
const shot = (name: string) => ({ path: path.join(SHOTS, `${name}.png`) });

test.describe("第二轮模块交互(部署实例 192.168.6.236)", () => {
  test.slow();

  test("订阅管理:新建→立即检查→暂停/恢复→取消清理", async ({ page }) => {
    // 选一个未订阅的电影 tmdb
    const resp = await page.request.get("/api/v1/subscriptions");
    const subs = (await resp.json()) as Array<{ tmdb_id: number; media_type: string; status: string }>;
    const used = new Set(subs.map((s) => `${s.media_type}:${s.tmdb_id}`));
    const candidate = [27205, 155, 157336, 603, 157350].find((id) => !used.has(`movie:${id}`)) ?? 27205;

    await page.goto("/subscriptions");
    await expect(page.getByText("订阅关注的影视，自动检查新资源并推送通知。")).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "新建订阅" }).click();
    await page.locator("form.create-form input[type=number]").fill(String(candidate));
    await page.locator("form.create-form button[type=submit]").click();
    const card = page.locator(".subscription-card", { hasText: `TMDB #${candidate}` }).first();
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.locator('.status-tag[data-status="active"]')).toBeVisible({ timeout: 15_000 });

    // 立即检查(真实上游搜索,放宽等待)
    const checkBtn = card.getByRole("button", { name: "检查" });
    await checkBtn.click();
    await expect(checkBtn).toBeEnabled({ timeout: 240_000 });
    // 检查后不得出现整页错误
    await expect(page.locator(".subscription-view .inline-alert, .subscription-view [role=alert]")).toHaveCount(0);

    // 暂停 → 恢复
    await card.getByRole("button", { name: "暂停" }).click();
    await expect(card.locator('.status-tag[data-status="paused"]')).toBeVisible({ timeout: 15_000 });
    await card.getByRole("button", { name: "恢复" }).click();
    await expect(card.locator('.status-tag[data-status="active"]')).toBeVisible({ timeout: 15_000 });

    // 取消清理
    await card.locator('button[title="取消订阅"]').click();
    await expect(card.locator('.status-tag[data-status="cancelled"]')).toBeVisible({ timeout: 15_000 });
    await page.screenshot({ ...shot("subscription-full-interaction"), fullPage: true });
  });

  test("详情页收藏:收藏→收藏页可见→取消收藏", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    const card = page.locator(".movie-card").first();
    await expect(card).toBeVisible({ timeout: 45_000 });
    const title = (await card.locator(".movie-card-copy strong").textContent())?.trim() ?? "";
    await card.click();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 60_000 });

    const favBtn = page.locator("button.favorite-inline");
    await expect(favBtn).toBeVisible({ timeout: 45_000 });
    if ((await favBtn.textContent())?.includes("已收藏")) {
      await favBtn.click(); // 先取消已有收藏,保证用例从"未收藏"开始
      await expect(favBtn).toContainText("收藏");
    }
    await favBtn.click();
    await expect(favBtn).toContainText("已收藏", { timeout: 15_000 });

    // 收藏页可见
    await page.goto("/favorites");
    await expect(page.getByRole("heading", { name: "收藏" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-card", { hasText: title.slice(0, 4) }).first()).toBeVisible({ timeout: 15_000 });

    // 从收藏页进详情取消收藏
    await page.locator(".movie-card", { hasText: title.slice(0, 4) }).first().click();
    await expect(page).toHaveURL(/\/movie\/\d+/, { timeout: 60_000 });
    const favBtn2 = page.locator("button.favorite-inline");
    await expect(favBtn2).toBeVisible({ timeout: 45_000 });
    await favBtn2.click();
    await expect(favBtn2).toContainText("收藏", { timeout: 15_000 });

    // 收藏页不再可见
    await page.goto("/favorites");
    await expect(page.getByRole("heading", { name: "收藏" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".movie-card", { hasText: title.slice(0, 4) }).first()).toHaveCount(0, { timeout: 15_000 });
    await page.screenshot({ ...shot("favorite-toggle"), fullPage: true });
  });

  test("电影目录:排序与年份筛选切换无错误", async ({ page }) => {
    await page.goto("/movies");
    await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible({ timeout: 45_000 });

    // 排序切换
    await page.getByRole("tab", { name: "评分优先" }).click();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    await expect(page.locator(".movie-card").first()).toBeVisible({ timeout: 45_000 });

    // 年份筛选(非"全部"的按钮)
    const yearBtn = page.locator(".filter-row", { hasText: "年份" }).locator("button").nth(1);
    await yearBtn.click();
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    // 有结果或无结果空态,但不得报错
    await expect(page.locator(".movie-card, .empty-state").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".catalog-error")).toHaveCount(0);
    await page.screenshot({ ...shot("catalog-filters"), fullPage: true });
  });

  test("详情页资源:来源筛选/排序/分页切换无错误", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("searchbox", { name: "搜索电影或电视剧" }).fill("权力的游戏");
    await page.getByRole("button", { name: "提交搜索" }).click();
    await expect(page.getByRole("heading", { name: "搜索结果" })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".catalog-loading")).toHaveCount(0, { timeout: 60_000 });
    await page.locator(".movie-card").first().click();
    await expect(page).toHaveURL(/\/tv\/\d+/, { timeout: 60_000 });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });

    const surface = page.locator(".resource-surface");
    // 来源筛选:磁力
    await surface.getByRole("button", { name: /^磁力/ }).click();
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    await expect(page.getByText("资源分页暂不可用")).toHaveCount(0);

    // 排序:大小
    await surface.getByLabel("资源排序").selectOption({ label: "大小" });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    await expect(page.getByText("资源分页暂不可用")).toHaveCount(0);

    // 每页 50
    await surface.getByLabel("资源每页数量").selectOption({ label: "50" });
    await expect(page.locator(".resource-loading-local")).toHaveCount(0, { timeout: 240_000 });
    await expect(page.locator(".resource-table tbody tr, .resource-card, .season-empty").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("resource-filters"), fullPage: true });
  });

  test("日志页:分类与等级筛选无错误", async ({ page }) => {
    await page.goto("/logs");
    await expect(page.getByRole("heading", { name: "日志" })).toBeVisible({ timeout: 30_000 });
    const logRow = page.locator(".settings-log-table tbody tr").first();
    await expect(logRow).toBeVisible({ timeout: 30_000 });

    const categorySelect = page.locator("label", { hasText: "分类" }).locator("select");
    const optionCount = await categorySelect.locator("option").count();
    if (optionCount > 1) {
      await categorySelect.selectOption({ index: 1 });
      const logRow = page.locator(".settings-log-table tbody tr").first();
    await expect(logRow).toBeVisible({ timeout: 30_000 });
    }
    const levelSelect = page.locator("label", { hasText: "等级" }).locator("select");
    await levelSelect.selectOption({ index: 1 }).catch(() => {});
    await expect(page.locator(".settings-log-table tbody tr, .settings-empty-block").first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ ...shot("logs-filters"), fullPage: true });
  });

  test("通知中心:偏好开关往返并还原", async ({ page }) => {
    await page.goto("/notifications");
    await expect(page.locator(".notification-list, .notification-empty, .notification-item").first()).toBeVisible({ timeout: 30_000 });
    const pref = page.locator("label.notification-preference", { hasText: "接收站内通知" }).locator("input[type=checkbox]");
    await expect(pref).toBeVisible({ timeout: 30_000 });
    // 等待偏好从服务端加载完成(初始 ref 为 true,加载后可能翻转)
    await page.waitForTimeout(2_500);
    const initial = (await pref.isChecked()) as boolean;
    await expect.poll(async () => pref.isChecked(), { timeout: 15_000 }).toBe(initial);
    await pref.setChecked(!initial);
    await expect.poll(async () => pref.isChecked(), { timeout: 15_000 }).toBe(!initial);
    await page.reload();
    await expect(pref).toBeVisible({ timeout: 30_000 });
    await expect.poll(async () => pref.isChecked(), { timeout: 15_000 }).toBe(!initial);
    await pref.setChecked(initial);
    await expect.poll(async () => pref.isChecked(), { timeout: 15_000 }).toBe(initial);
    await page.reload();
    await expect(pref).toBeVisible({ timeout: 30_000 });
    await expect.poll(async () => pref.isChecked(), { timeout: 15_000 }).toBe(initial);
    await page.screenshot({ ...shot("notification-prefs"), fullPage: true });
  });
});
