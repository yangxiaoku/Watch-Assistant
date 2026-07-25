import { expect, test, type Page } from "@playwright/test";

const baseMovie = {
  tmdb_id: 27205,
  media_type: "movie" as const,
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "梦境中的梦境。",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [878],
  vote_average: 8.4,
};

function catalogMovie(pageNumber: number, index: number, mediaType: "movie" | "tv" = "movie", label = "") {
  return {
    ...baseMovie,
    tmdb_id: 27205 + pageNumber * 100 + index,
    media_type: mediaType,
    title: `${label || (mediaType === "tv" ? "剧集" : "电影")}第 ${pageNumber} 页 ${index + 1}`,
    original_title: `Catalog ${pageNumber}-${index}`,
  };
}

async function installCatalogMocks(page: Page) {
  const discoverRequests: Array<{ page: number; genre: number | null; year: number | null; sort: string }> = [];
  const popularRequests: number[] = [];
  const searchRequests: Array<{ query: string; page: number }> = [];
  const failedPages = new Set<number>();
  const delayedGenres = new Map<number, number>();

  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/media/discover?**", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const requestedPage = Number(params.get("page") ?? "1");
    const genre = params.get("genre_id");
    const requestedGenre = genre === null ? null : Number(genre);
    const requestedYear = params.get("year");
    discoverRequests.push({
      page: requestedPage,
      genre: requestedGenre,
      year: requestedYear === null ? null : Number(requestedYear),
      sort: params.get("sort") ?? "popular",
    });
    const delay = requestedGenre === null ? 0 : delayedGenres.get(requestedGenre) ?? 0;
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    if (failedPages.has(requestedPage)) {
      await route.fulfill({ status: 500, json: { detail: "目录暂时不可用" } });
      return;
    }
    const mediaType = params.get("media_type") === "tv" ? "tv" : "movie";
    await route.fulfill({
      json: {
        results: Array.from({ length: 12 }, (_, index) => catalogMovie(requestedPage, index, mediaType, requestedGenre === null ? "" : `类型 ${requestedGenre}`)),
        page: requestedPage,
        total_pages: requestedPage === 500 ? 999 : 3,
        total_results: 60,
      },
    });
  });
  await page.route("**/api/v1/movies/popular?**", (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get("page") ?? "1");
    popularRequests.push(requestedPage);
    return route.fulfill({
      json: {
        results: Array.from({ length: 12 }, (_, index) => catalogMovie(requestedPage, index)),
        page: requestedPage,
        total_pages: requestedPage === 500 ? 999 : 3,
        total_results: 60,
      },
    });
  });
  await page.route("**/api/v1/media/search?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const requestedPage = Number(params.get("page") ?? "1");
    searchRequests.push({ query: params.get("query") ?? "", page: requestedPage });
    return route.fulfill({
      json: {
        results: Array.from({ length: 12 }, (_, index) => catalogMovie(requestedPage, index, "movie", params.get("query") ?? "搜索")),
        page: requestedPage,
        total_pages: 3,
        total_results: 60,
      },
    });
  });
  await page.route("**/api/v1/search", (route) =>
    route.fulfill({
      json: {
        movie: baseMovie,
        results: [],
        warnings: [],
        cached: false,
        cache_age_seconds: null,
      },
    }),
  );

  return { discoverRequests, popularRequests, searchRequests, failedPages, delayedGenres };
}

async function expectNoHorizontalOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test("restores filtered page 2 after refresh and captures desktop/mobile catalog", async ({ page }, testInfo) => {
  const mocks = await installCatalogMocks(page);
  await page.goto("/movies?page=2&genre=28&year=2024&sort=rating");

  await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
  expect(mocks.discoverRequests.at(-1)).toEqual({ page: 2, genre: 28, year: 2024, sort: "rating" });
  await expect(page).toHaveURL(/\/movies\?page=2&genre=28&year=2024&sort=rating$/);
  await expectNoHorizontalOverflow(page);

  await page.reload();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
  await expect(page.locator(".movie-card").first()).toContainText("第 2 页");
  expect(mocks.discoverRequests.at(-1)).toEqual({ page: 2, genre: 28, year: 2024, sort: "rating" });
  await page.screenshot({ path: testInfo.outputPath(`catalog-${testInfo.project.name}.png`), fullPage: true });
  await expectNoHorizontalOverflow(page);
});

test("returns from page 2 detail with the cached list and scroll position", async ({ page }) => {
  const mocks = await installCatalogMocks(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/movies");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();

  await page.evaluate(() => window.scrollTo({ top: 380, behavior: "auto" }));
  const savedScroll = await page.evaluate(() => window.scrollY);
  await page.getByRole("button", { name: /查看 电影第 2 页/ }).first().click();
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await page.getByRole("button", { name: "返回浏览" }).click();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
  await expect(page).toHaveURL(/\/movies\?page=2$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(savedScroll);
  expect(mocks.discoverRequests).toHaveLength(2);
  await expectNoHorizontalOverflow(page);
});

test("restores catalog state on browser back and forward", async ({ page }) => {
  await installCatalogMocks(page);
  await page.goto("/movies");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();

  await page.goBack();
  await expect(page.getByText("第 1 / 3 页 · 共 60 条")).toBeVisible();
  await expect(page).toHaveURL(/\/movies$/);
  await page.goForward();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
  await expect(page).toHaveURL(/\/movies\?page=2$/);
});

test("resets filters to page 1 and ignores an older response", async ({ page }) => {
  const mocks = await installCatalogMocks(page);
  mocks.delayedGenres.set(28, 220);
  mocks.delayedGenres.set(12, 10);
  await page.goto("/movies?page=2");
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();

  const action = page.getByRole("button", { name: "动作", exact: true });
  const adventure = page.getByRole("button", { name: "冒险", exact: true });
  await action.click();
  await adventure.click();
  await expect(page).toHaveURL(/\/movies\?genre=12$/);
  await expect(page.getByRole("button", { name: "查看 类型 12第 1 页 1", exact: true })).toBeVisible();
  await page.waitForTimeout(260);
  expect(mocks.discoverRequests.at(-1)?.genre).toBe(12);
  await expect(page.locator(".movie-card").first()).toContainText("类型 12");
});

test("keeps the old list and page when a catalog request fails", async ({ page }) => {
  const mocks = await installCatalogMocks(page);
  await page.goto("/movies");
  await expect(page.getByText("第 1 / 3 页 · 共 60 条")).toBeVisible();
  mocks.failedPages.add(2);

  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("目录暂时不可用")).toBeVisible();
  await expect(page.getByText("第 1 / 3 页 · 共 60 条")).toBeVisible();
  await expect(page.locator(".movie-card").first()).toContainText("第 1 页");
  await expect(page).toHaveURL(/\/movies$/);
});

test("does not request page 501 after the capped last page", async ({ page }) => {
  const mocks = await installCatalogMocks(page);
  await page.goto("/popular?page=500");
  await expect(page.getByRole("heading", { name: "本周热门" })).toBeVisible();
  await expect(page.getByText("第 500 / 500 页 · 共 60 条")).toBeVisible();
  const before = mocks.popularRequests.length;
  await expect(page.getByRole("button", { name: "下一页" })).toBeDisabled();
  expect(mocks.popularRequests).toHaveLength(before);
  await expectNoHorizontalOverflow(page);
});

test("restores a search query and page from the URL", async ({ page }) => {
  const mocks = await installCatalogMocks(page);
  await page.goto("/search?query=the%20bear&page=2");
  await expect(page.getByRole("heading", { name: "“the bear”的搜索结果" })).toBeVisible();
  await expect(page.getByText("第 2 / 3 页 · 共 60 条")).toBeVisible();
  expect(mocks.searchRequests.at(-1)).toEqual({ query: "the bear", page: 2 });
  await page.reload();
  await expect(page.getByRole("heading", { name: "“the bear”的搜索结果" })).toBeVisible();
  await expect(page).toHaveURL(/\/search\?page=2&query=the\+bear$/);
  await expectNoHorizontalOverflow(page);
});
