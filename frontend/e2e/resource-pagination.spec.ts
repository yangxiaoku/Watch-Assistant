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
  const visibleResources = testInfo.project.name === "mobile" ? page.locator(".resource-cards:visible .resource-card") : page.locator(".resource-table-wrap:visible tbody tr");
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
    if (url.searchParams.get("query") === "old") await new Promise((resolve) => setTimeout(resolve, 250));
    return route.fulfill({ json: { ...pageResponse(1), items: [resource(`query-${url.searchParams.get("query") ?? "all"}`, 1)] } });
  });
  await page.goto("/movie/27205");
  await page.getByLabel("资源名称搜索").fill("old");
  await page.waitForTimeout(280);
  await page.getByLabel("资源名称搜索").fill("new");
  await expect(page.locator(".resource-title").first()).toContainText("query-new");
  await page.getByRole("button", { name: "字幕 80" }).click();
  await page.locator('select[aria-label="资源排序"]').selectOption("seeders");
  await page.locator('select[aria-label="资源每页数量"]').selectOption("50");
  await expect.poll(() => requests.some((url) => url.searchParams.get("quality") === "subtitle" && url.searchParams.get("sort") === "seeders" && url.searchParams.get("page_size") === "50")).toBe(true);
  await expect(page.locator(".resource-title").first()).not.toContainText("query-old");
});

test("falls back to the POST result when the resource snapshot is missing", async ({ page }) => {
  await mockShell(page);
  await page.route("**/api/v1/media/movie/27205/resources**", (route) => route.fulfill({ status: 404, json: { detail: "resource_snapshot_not_found" } }));
  await page.goto("/movie/27205");
  await expect(page.getByRole("status")).toContainText("分页暂不可用");
  await expect(page.locator(".resource-title").first()).toContainText("legacy-0");
  await expect(page.getByRole("button", { name: "下一页" })).toHaveCount(0);
});

test("pagination does not repeat the automatic inspection batch", async ({ page }) => {
  let inspectPostCount = 0;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }));
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
