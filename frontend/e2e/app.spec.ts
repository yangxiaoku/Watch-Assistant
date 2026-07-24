import { expect, test } from "@playwright/test";

test("login, popular browsing, and resource detail remain usable", async ({ page }) => {
  let authenticated = false;
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    authenticated
      ? route.fulfill({
          json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" },
        })
      : route.fulfill({ status: 401, json: { detail: "unauthorized" } }),
  );
  await page.route("**/api/v1/auth/login", (route) => {
    authenticated = true;
    return route.fulfill({ json: { csrf_token: "csrf-test" } });
  });
  await page.route("**/api/v1/movies/popular", (route) =>
    route.fulfill({
      json: {
        results: [
          {
            tmdb_id: 27205,
            title: "盗梦空间",
            original_title: "Inception",
            release_year: 2010,
            overview: "梦境中的梦境。",
            poster_path: null,
            vote_average: 8.4,
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/search", (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
    return route.fulfill({
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
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "进入观影工作台" })).toBeVisible();
  await page.getByLabel("Web 密码").fill("test-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "当前热门" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "当前热门" })).toBeVisible();
  await page.getByRole("button", { name: "查看 盗梦空间" }).click();

  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page).toHaveURL(/\/movie\/27205$/);
  await expect(page.locator("tbody tr").filter({ hasText: "Inception 2010 2160p" })).toContainText(
    "未知",
  );
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
});
