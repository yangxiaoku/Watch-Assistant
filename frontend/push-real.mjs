import { chromium } from "@playwright/test";
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30_000);
const logs = [];
page.on("console", (m) => { if (m.type() === "error") logs.push(m.text().slice(0, 120)); });

await page.goto("http://192.168.6.236:8115/");
await page.locator("#username").fill(process.env.WA_E2E_USER);
await page.locator("#password").fill(process.env.WA_E2E_PASSWORD);
await page.getByRole("button", { name: "登录" }).click();
await page.waitForTimeout(3000);

// 搜索沙丘并进入详情
const search = page.getByRole("searchbox", { name: "搜索电影或电视剧" });
await search.fill("星际穿越");
await page.getByRole("button", { name: "提交搜索" }).click();
await page.waitForTimeout(6000);
const card = page.locator(".movie-card").first();
await card.click();
await page.waitForTimeout(8000);
console.log("detail URL:", page.url());

// 等资源表出现，选第一个磁力行
await page.locator(".resource-surface").waitFor({ timeout: 60_000 });
const magnetRows = page.locator(".resource-table tbody tr, .resource-cards article").filter({ hasText: "磁力" });
await magnetRows.first().waitFor({ timeout: 60_000 });
// 选一个 1080p 磁力行,避免命中此前已推送过的同一 infohash
const magnetRow = magnetRows.filter({ hasText: "1080p" }).first();
await magnetRow.waitFor({ timeout: 60_000 });
const name = (await magnetRow.textContent())?.slice(0, 60);
console.log("pushing magnet:", JSON.stringify(name));

// 点击推送
await magnetRow.getByRole("button", { name: "推送下载" }).click();
await page.waitForTimeout(2500);
const toasts = await page.locator(".toast-message").allTextContents();
console.log("toast after push:", JSON.stringify(toasts));

// 轮询最新任务状态
const states = [];
for (let i = 0; i < 40; i++) {
  await page.waitForTimeout(15_000);
  const tasks = await page.evaluate(async () => {
    const res = await fetch("/api/v1/tasks");
    if (!res.ok) return [];
    return await res.json();
  });
  const newest = tasks[0];
  if (newest) {
    const rec = { state: newest.state, error_code: newest.error_code, ts: new Date().toISOString().slice(11, 19) };
    if (!states.length || states[states.length - 1].state !== rec.state || states[states.length - 1].error_code !== rec.error_code) {
      states.push(rec);
      console.log("task state:", JSON.stringify(rec), "id:", newest.id.slice(0, 12), "state_zh:", newest.state_zh);
    }
    if (["available", "failed", "cancelled"].includes(newest.state)) break;
  } else {
    console.log("no tasks yet");
  }
}
console.log("FINAL states:", JSON.stringify(states));
console.log("console errors:", JSON.stringify(logs));
await page.screenshot({ path: "/tmp/push-result.png", fullPage: false });
await browser.close();
