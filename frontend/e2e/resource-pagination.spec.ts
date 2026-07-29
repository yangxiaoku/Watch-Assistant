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

function resource(id: string, page: number) {
  return {
    resource_id: id,
    kind: "magnet",
    name: `Inception page ${page} ${id}`,
    size_bytes: null,
    seeders: page,
    source: "test",
    captured_at: "2026-07-24T10:00:00Z",
    rank_score: 90,
    relevance_score: 80,
    completeness_score: 70,
  };
}

function pageResponse(page: number, pageSize = 25) {
  return {
    items: Array.from({ length: Math.min(pageSize, 501 - (page - 1) * pageSize) }, (_, index) => resource(`resource-${page}-${index}`, page)),
    page,
    page_size: pageSize,
    total: 501,
    total_pages: Math.ceil(501 / pageSize),
    facets: { magnet: 500, share: 1, "4k": 100, "1080p": 300, "720p": 50, subtitle: 80 },
    snapshot_revision: "snapshot-1",
  };
}

function inspectionResult(resourceId: string, status: "verified" | "failed" = "verified") {
  return {
    resource_id: resourceId,
    infohash: status === "verified" ? `hash-${resourceId}` : null,
    status,
    total_size_bytes: status === "verified" ? 1024 : 0,
    file_count: status === "verified" ? 1 : 0,
    video_file_count: status === "verified" ? 1 : 0,
    video_size_bytes: status === "verified" ? 1024 : 0,
    subtitle_count: 0,
    sample_count: 0,
    largest_video_name: null,
    content_summary: null,
    error_code: status === "verified" ? null : "FAILED",
  };
}

async function mockShell(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: {
    movie,
    results: Array.from({ length: 30 }, (_, index) => resource(`legacy-${index}`, 1)),
    warnings: [],
    cached: false,
    cache_age_seconds: null,
  } }));
}

test("uses resource pages beyond the POST result limit and restores URL state", async ({ page }, testInfo) => {
  const resourceRequests: string[] = [];
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => {
    resourceRequests.push(route.request().url());
    const params = new URL(route.request().url()).searchParams;
    return route.fulfill({ json: pageResponse(Number(params.get("page") ?? 1), Number(params.get("page_size") ?? 25)) });
  });

  await page.goto("/movie/27205");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  const visibleResources = testInfo.project.name.startsWith("mobile") ? page.locator(".resource-cards:visible .resource-card") : page.locator(".resource-table-wrap:visible tbody tr");
  await expect(visibleResources).toHaveCount(25);
  await expect(page.locator(".resource-heading-copy h2")).toContainText("501");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".resource-title").first()).toContainText("page 2");
  await expect(page).toHaveURL(/resource_page=2/);
  await page.reload();
  await expect(page.locator(".resource-title").first()).toContainText("page 2");
  expect(resourceRequests.some((url) => new URL(url).searchParams.get("page") === "2")).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("resource-pagination.png"), fullPage: true });
});

test("sends backend filters and ignores a stale delayed response", async ({ page }) => {
  const requests: URL[] = [];
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    if (url.searchParams.get("query") === "old") await new Promise((resolve) => setTimeout(resolve, 120));
    return route.fulfill({ json: { ...pageResponse(1), items: [resource(`query-${url.searchParams.get("query") ?? "all"}`, 1)] } });
  });
  await page.goto("/movie/27205");
  await page.getByLabel("资源名称搜索").fill("old");
  await page.waitForTimeout(280);
  await page.getByLabel("资源名称搜索").fill("new");
  await expect(page.getByLabel("资源名称搜索")).toHaveValue("new");
  await page.waitForTimeout(150);
  await expect(page.getByLabel("资源名称搜索")).toHaveValue("new");
  await expect(page).not.toHaveURL(/resource_query=old/);
  await expect(page.locator(".resource-title").first()).not.toContainText("query-old");
  await expect(page.locator(".resource-title").first()).toContainText("query-new");
  await page.getByRole("button", { name: "字幕 80" }).click();
  await page.locator('select[aria-label="资源排序"]').selectOption("seeders");
  await page.locator('select[aria-label="资源每页数量"]').selectOption("50");
  await expect.poll(() => requests.some((url) => url.searchParams.get("quality") === "subtitle" && url.searchParams.get("sort") === "seeders" && url.searchParams.get("page_size") === "50")).toBe(true);
  await expect(page.locator(".resource-title").first()).not.toContainText("query-old");
});

test("retries the current resource route after canceling a pending initial page", async ({ page }) => {
  const requests: URL[] = [];
  let releaseInitial!: () => void;
  const initialGate = new Promise<void>((resolve) => { releaseInitial = resolve; });
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", async (route) => {
    requests.push(new URL(route.request().url()));
    if (requests.length === 1) await initialGate;
    try {
      await route.fulfill({ json: pageResponse(1) });
    } catch {
      // The initial request is expected to be aborted by the query edit.
    }
  });

  await page.goto("/movie/27205");
  await expect(page.locator(".resource-surface")).toHaveAttribute("aria-busy", "true");
  const input = page.getByLabel("资源名称搜索");
  await input.fill("temporary");
  await page.waitForTimeout(80);
  await input.fill("");
  await expect(input).toHaveValue("");
  await expect.poll(() => requests.length).toBe(2);
  await expect(page).not.toHaveURL(/resource_query=temporary/);
  await expect(page.locator(".resource-surface")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByRole("button", { name: "下一页" }).first()).not.toBeDisabled();
  await expect(page.locator(".resource-title").first()).toContainText("page 1");
  expect(requests.every((request) => request.searchParams.get("query") !== "temporary")).toBe(true);
  releaseInitial();
  await page.waitForTimeout(30);
});

test("does not refetch when a canceled draft returns to an existing page response", async ({ page }) => {
  let requestCount = 0;
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => {
    requestCount += 1;
    return route.fulfill({ json: pageResponse(1) });
  });

  await page.goto("/movie/27205");
  await expect.poll(() => requestCount).toBe(1);
  const input = page.getByLabel("资源名称搜索");
  await input.fill("temporary");
  await page.waitForTimeout(80);
  await input.fill("");
  await expect(input).toHaveValue("");
  await page.waitForTimeout(320);
  expect(requestCount).toBe(1);
  await expect(page).not.toHaveURL(/resource_query=temporary/);
  await expect(page.locator(".resource-surface")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator(".resource-title").first()).toContainText("page 1");
});

test("falls back to the POST result when the resource snapshot is missing", async ({ page }) => {
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ status: 404, json: { detail: "resource_snapshot_not_found" } }));
  await page.goto("/movie/27205");
  await expect(page.locator(".resource-page-notice")).toContainText("分页暂不可用");
  await expect(page.locator(".resource-title").first()).toContainText("legacy-0");
  await expect(page.getByRole("button", { name: "下一页" })).toHaveCount(0);
});

test("does not fabricate a catalog return for a direct detail URL or invalid state", async ({ page }) => {
  await mockShell(page);
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: {
    popular: [movie],
    now_playing: [movie],
    upcoming: [movie],
    top_rated: [movie],
    tv_popular: [movie],
    tv_on_the_air: [movie],
    tv_top_rated: [movie],
  } }));
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ json: pageResponse(1) }));

  await page.goto("/movie/27205");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await page.getByRole("button", { name: "返回浏览" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "正在热映" })).toBeVisible();

  await page.goto("/movie/27205");
  await page.evaluate(() => window.history.replaceState({
    catalogDetailEntry: true,
    catalog: { view: "movies", query: "", page: 0, sort: "popular" },
    catalogScrollY: -1,
    catalogBackDelta: 999,
  }, "", window.location.href));
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await page.getByRole("button", { name: "返回浏览" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "正在热映" })).toBeVisible();
});

test("pagination does not repeat the automatic inspection batch", async ({ page }) => {
  let inspectPostCount = 0;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true, inspection_auto_start_enabled: true } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: { movie, results: Array.from({ length: 30 }, (_, index) => resource(`legacy-${index}`, 1)), warnings: [], cached: false, cache_age_seconds: null } }));
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ json: pageResponse(Number(new URL(route.request().url()).searchParams.get("page") ?? 1)) }));
  await page.route("**/api/v1/resources/inspect", (route) => {
    inspectPostCount += 1;
    const ids = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    return route.fulfill({ json: { batch_id: "batch-1", status: "completed", submitted_count: ids.length, completed_count: ids.length, results: ids.map((resourceId) => ({ resource_id: resourceId, infohash: "hash", status: "verified", total_size_bytes: 1024, file_count: 1, video_file_count: 1, video_size_bytes: 1024, subtitle_count: 0, sample_count: 0, largest_video_name: null, content_summary: null, error_code: null })) } });
  });
  await page.goto("/movie/27205");
  await expect.poll(() => inspectPostCount).toBe(1);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".resource-title").first()).toContainText("page 2");
  expect(inspectPostCount).toBe(1);
});

test("disabled auto inspection stays quiet until the user starts an eight-item batch", async ({ page }) => {
  let inspectPostCount = 0;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true, inspection_auto_start_enabled: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: { movie, results: Array.from({ length: 30 }, (_, index) => resource(`legacy-${index}`, 1)), warnings: [], cached: false, cache_age_seconds: null } }));
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ json: pageResponse(Number(new URL(route.request().url()).searchParams.get("page") ?? 1)) }));
  await page.route("**/api/v1/resources/inspect", (route) => {
    inspectPostCount += 1;
    const ids = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    expect(ids).toHaveLength(8);
    return route.fulfill({ json: { batch_id: "manual-batch", status: "completed", submitted_count: ids.length, completed_count: ids.length, results: ids.map((resourceId) => inspectionResult(resourceId)) } });
  });
  await page.goto("/movie/27205");
  await expect(page.getByRole("button", { name: "开始检测" })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".resource-title").first()).toContainText("page 2");
  expect(inspectPostCount).toBe(0);
  await page.getByRole("button", { name: "开始检测" }).click();
  await expect.poll(() => inspectPostCount).toBe(1);
});

test("corrects a direct page 500 URL to the backend last page once", async ({ page }) => {
  const requests: number[] = [];
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get("page") ?? 1);
    requests.push(requestedPage);
    return route.fulfill({ json: {
      items: [resource("last-page", 21)],
      page: requestedPage === 21 ? 500 : 500,
      page_size: 25,
      total: 501,
      total_pages: 21,
      facets: { magnet: 500, share: 1, "4k": 100, "1080p": 300, "720p": 50, subtitle: 80 },
      snapshot_revision: "snapshot-1",
    } });
  });

  await page.goto("/movie/27205?resource_page=500");
  await expect(page.locator(".resource-title").first()).toContainText("last-page");
  await expect(page).toHaveURL(/resource_page=21/);
  expect(requests).toEqual([500, 21]);
  await expect(page.locator(".resource-pagination")).toContainText("第 21 / 21 页");
});

test("starts the automatic batch from the visible initial resource page", async ({ page }) => {
  const inspected: string[][] = [];
  const pageItems = Array.from({ length: 25 }, (_, index) => resource(`page-two-${index}`, 2));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true, inspection_auto_start_enabled: true } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: { movie, results: Array.from({ length: 30 }, (_, index) => resource(`legacy-${index}`, 1)), warnings: [], cached: false, cache_age_seconds: null } }));
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ json: { ...pageResponse(2), items: pageItems } }));
  await page.route("**/api/v1/resources/inspect", (route) => {
    const ids = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    inspected.push(ids);
    return route.fulfill({ json: { batch_id: "page-two-batch", status: "completed", submitted_count: ids.length, completed_count: ids.length, results: ids.map((id) => inspectionResult(id)) } });
  });

  await page.goto("/movie/27205?resource_page=2");
  await expect.poll(() => inspected.length).toBe(1);
  expect(inspected[0]).toHaveLength(8);
  expect(inspected[0].every((id) => id.startsWith("page-two-"))).toBe(true);
});

test("keeps unique inspection quota across pages and retries failures after the quota is full", async ({ page }) => {
  const inspected: string[][] = [];
  const failedIds = new Set<string>();
  const pageOne = Array.from({ length: 25 }, (_, index) => resource(`page-one-${index}`, 1));
  const pageTwo = Array.from({ length: 25 }, (_, index) => resource(`page-two-${index}`, 2));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true, inspection_auto_start_enabled: true } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: { movie, results: pageOne, warnings: [], cached: false, cache_age_seconds: null } }));
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get("page") ?? 1);
    return route.fulfill({ json: { ...pageResponse(requestedPage), items: requestedPage === 1 ? pageOne : pageTwo, total: 50, total_pages: 2 } });
  });
  await page.route("**/api/v1/resources/inspect", (route) => {
    const ids = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    inspected.push(ids);
    const isRetry = ids.every((id) => failedIds.has(id));
    if (!isRetry) ids.forEach((id) => failedIds.add(id));
    return route.fulfill({ json: { batch_id: `batch-${inspected.length}`, status: isRetry ? "completed" : "partial", submitted_count: ids.length, completed_count: ids.length, results: ids.map((id) => inspectionResult(id, isRetry ? "verified" : "failed")) } });
  });

  await page.goto("/movie/27205");
  await expect.poll(() => inspected.length).toBe(1);
  for (let index = 0; index < 3; index += 1) {
    await page.getByRole("button", { name: "检测更多" }).click();
    await expect.poll(() => inspected.length).toBe(index + 2);
  }
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".resource-title").first()).toContainText("page-two-0");
  await page.getByRole("button", { name: "检测更多" }).click();
  await expect.poll(() => inspected.length).toBe(5);
  expect(new Set(inspected.flat()).size).toBeLessThanOrEqual(30);
  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  const lastPageBatch = inspected[4];
  await page.getByRole("button", { name: "重试失败项" }).click();
  await expect.poll(() => inspected.length).toBe(6);
  expect(inspected[5]).toEqual(lastPageBatch);
});

test("returns from an internal resource route to the original catalog entry", async ({ page }) => {
  const catalogRequests: number[] = [];
  await page.emulateMedia({ reducedMotion: "reduce" });
  const catalogMovie = { ...movie, tmdb_id: 27206, title: "目录电影" };
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/media/discover?**", (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get("page") ?? 1);
    catalogRequests.push(requestedPage);
    return route.fulfill({ json: {
      results: Array.from({ length: 12 }, (_, index) => ({ ...catalogMovie, tmdb_id: 27206 + index, title: `目录第 ${requestedPage} 页 ${index + 1}` })),
      page: requestedPage,
      total_pages: 2,
      total_results: 24,
    } });
  });
  await page.route("**/api/v1/search", (route) => route.fulfill({ json: {
    movie: catalogMovie,
    results: Array.from({ length: 30 }, (_, index) => resource(`legacy-${index}`, 1)),
    warnings: [],
    cached: false,
    cache_age_seconds: null,
  } }));
  await page.route("**/api/v1/media/movie/27206/resources**", (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get("page") ?? 1);
    return route.fulfill({ json: {
      items: Array.from({ length: 25 }, (_, index) => resource(`detail-${requestedPage}-${index}`, requestedPage)),
      page: requestedPage,
      page_size: 25,
      total: 50,
      total_pages: 2,
      facets: { magnet: 50, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 },
      snapshot_revision: "catalog-history-snapshot",
    } });
  });

  await page.goto("/movies");
  await expect(page.getByText("第 1 / 2 页 · 共 24 条")).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 / 2 页 · 共 24 条")).toBeVisible();
  await page.waitForTimeout(50);
  await page.evaluate(() => window.scrollTo({ top: 64, behavior: "auto" }));
  const savedScroll = await page.evaluate(() => window.scrollY);
  await page.getByRole("button", { name: /查看 目录第 2 页/ }).first().click();
  await expect(page.getByRole("heading", { name: "目录电影" })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/resource_page=2/);
  await expect(page.locator(".resource-title").first()).toContainText("page 2");
  await page.locator('select[aria-label="资源排序"]').selectOption("relevance");
  await expect(page).toHaveURL(/resource_sort=relevance/);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/resource_page=2.*resource_sort=relevance|resource_sort=relevance.*resource_page=2/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "目录电影" })).toBeVisible();
  await page.getByRole("button", { name: "返回浏览" }).click();
  await expect(page).toHaveURL(/\/movies\?page=2$/);
  await expect(page.getByText("第 2 / 2 页 · 共 24 条")).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(savedScroll);
  expect(catalogRequests).toEqual([1, 2, 2]);

  await page.goBack();
  await expect(page.getByText("第 1 / 2 页 · 共 24 条")).toBeVisible();
  await page.goForward();
  await expect(page.getByText("第 2 / 2 页 · 共 24 条")).toBeVisible();
  expect(catalogRequests).toEqual([1, 2, 2, 1]);
});
