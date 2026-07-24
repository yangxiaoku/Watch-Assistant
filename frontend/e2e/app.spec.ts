import { expect, test } from "@playwright/test";

test("login and search remain usable without horizontal overflow", async ({ page }) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ status: 401, json: { detail: "unauthorized" } }),
  );
  await page.route("**/api/v1/auth/login", (route) =>
    route.fulfill({ json: { csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) =>
    route.fulfill({
      json: {
        movie: {
          tmdb_id: 27205,
          title: "盗梦空间",
          original_title: "Inception",
          release_year: 2010,
          overview: "梦境中的梦境。",
          poster_path: null,
        },
        results: [
          {
            resource_id: "res_test",
            kind: "magnet",
            name: "Inception 2010 2160p",
            size_bytes: null,
            seeders: null,
            source: "plugin:test",
            captured_at: "2026-07-24T10:00:00Z",
          },
        ],
        warnings: [],
        cached: false,
        cache_age_seconds: 0,
      },
    }),
  );

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "进入观影工作台" })).toBeVisible();
  await page.getByLabel("Web 密码").fill("test-password");
  await page.getByRole("button", { name: "登录" }).click();
  await page.getByLabel("TMDB 电影 ID").fill("27205");
  await page.getByRole("button", { name: "搜索资源" }).click();

  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page.locator("tbody tr").filter({ hasText: "Inception 2010 2160p" })).toContainText(
    "未知",
  );
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
});
