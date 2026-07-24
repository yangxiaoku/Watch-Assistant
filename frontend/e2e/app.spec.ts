import { expect, test } from "@playwright/test";

const movie = {
  tmdb_id: 27205,
  media_type: "movie",
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "梦境中的梦境。",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [878],
  vote_average: 8.4,
};

const show = {
  ...movie,
  tmdb_id: 1399,
  media_type: "tv",
  title: "权力的游戏",
  original_title: "Game of Thrones",
};

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
  await page.route("**/api/v1/movies/home", (route) =>
    route.fulfill({
      json: {
        popular: [movie],
        now_playing: [movie],
        upcoming: [movie],
        top_rated: [movie],
        tv_popular: [show],
        tv_on_the_air: [show],
        tv_top_rated: [show],
      },
    }),
  );
  await page.route("**/api/v1/search", (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
    const request = route.request().postDataJSON() as { media_type: "movie" | "tv" };
    const selected = request.media_type === "tv" ? show : movie;
    return route.fulfill({
      json: {
        movie: selected,
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
  await page.route("**/api/v1/media/discover?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const requestedPage = Number(params.get("page") ?? "1");
    const selected = params.get("media_type") === "tv" ? show : movie;
    return route.fulfill({
      json: { results: [selected], page: requestedPage, total_pages: 3, total_results: 60 },
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "进入观影工作台" })).toBeVisible();
  await page.getByLabel("Web 密码").fill("test-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "正在热映" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "正在热映" })).toBeVisible();
  await page.locator(".feature-hero").click();

  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page).toHaveURL(/\/movie\/27205$/);
  await expect(page.locator("tbody tr").filter({ hasText: "Inception 2010 2160p" })).toContainText(
    "未知",
  );
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);

  await page.goto("/");
  const tvSection = page.getByRole("region", { name: "热播剧集" });
  await tvSection.getByRole("button", { name: "查看 权力的游戏" }).click();
  await expect(page.getByRole("heading", { name: "权力的游戏" })).toBeVisible();
  await expect(page).toHaveURL(/\/tv\/1399$/);

  await page.goto("/movies");
  await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();

  await page.getByRole("button", { name: "剧集", exact: true }).click();
  await expect(page.getByRole("heading", { name: "剧集库" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 权力的游戏" })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
});
