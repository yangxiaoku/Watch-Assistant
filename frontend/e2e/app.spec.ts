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

function inspectionResult(resourceId: string, status: "verified" | "unsupported" | "timeout" | "failed") {
  return {
    resource_id: resourceId,
    infohash: status === "unsupported" ? null : `hash-${resourceId}`,
    status,
    total_size_bytes: status === "verified" ? 1073741824 : 0,
    file_count: status === "verified" ? 1 : 0,
    video_file_count: status === "verified" ? 1 : 0,
    video_size_bytes: status === "verified" ? 1000000000 : 0,
    subtitle_count: status === "verified" ? 1 : 0,
    sample_count: status === "verified" ? 1 : 0,
    largest_video_name: status === "verified" ? "episode.mkv" : null,
    content_summary: status === "verified" ? "verified" : null,
    error_code: status === "verified" ? null : status.toUpperCase(),
  };
}

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

test("automatically inspects eight magnets and continues without duplicates", async ({ page }, testInfo) => {
  const searchRequests: Array<Record<string, unknown>> = [];
  const inspectedBatches: string[][] = [];
  let inspectGetCount = 0;
  const tvWithSeasons = {
    ...show,
    seasons: [
      { season_number: 0, name: "特别篇", episode_count: 1, air_date: "2010-01-01", poster_path: null },
      { season_number: 1, name: "第 1 季", episode_count: 10, air_date: "2011-04-17", poster_path: null },
      { season_number: 2, name: "第 2 季", episode_count: 10, air_date: "2012-04-01", poster_path: null },
    ],
  };
  const resources = Array.from({ length: 31 }, (_, index) => ({
    resource_id: `magnet-${index}`,
    kind: "magnet",
    name: index === 0
      ? "权力的游戏 S01E01 2160P"
      : index === 1
        ? "权力的游戏 S01E02 1080P 字幕"
        : index === 2
          ? "权力的游戏 S01E03 720P"
          : `权力的游戏 S01E${String(index + 1).padStart(2, "0")}`,
    size_bytes: null,
    seeders: index,
    size_source: "pansou",
    seeders_source: "pansou",
    seeders_observed_at: "2026-07-24T10:00:00Z",
    source: "plugin:test",
    captured_at: "2026-07-24T10:00:00Z",
    rank_score: 90 - index,
    relevance_score: index === 0 ? 95 : null,
    completeness_score: null,
  }));

  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/media/discover?**", (route) =>
    route.fulfill({ json: { results: [movie], page: 1, total_pages: 1, total_results: 1 } }),
  );
  let delayNextSearch = false;
  await page.route("**/api/v1/search", async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    searchRequests.push(request);
    const selected = request.media_type === "tv";
    if (delayNextSearch) {
      delayNextSearch = false;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return route.fulfill({
      json: {
        movie: selected ? tvWithSeasons : movie,
        results: selected ? [...resources, {
          resource_id: "share-1",
          kind: "115_share",
          name: "115 分享资源",
          size_bytes: null,
          seeders: null,
          source: "share:test",
          captured_at: "2026-07-24T10:00:00Z",
        }] : [],
        warnings: [],
        cached: selected ? request.season_number === 0 : false,
        cache_age_seconds: null,
        selected_season: selected ? (request.season_number ?? null) : undefined,
      },
    });
  });
  await page.route("**/api/v1/resources/inspect", (route) => {
    const body = route.request().postDataJSON() as { resource_ids: string[] };
    inspectedBatches.push(body.resource_ids);
    const completed = inspectedBatches.length > 1;
    return route.fulfill({
      json: {
        batch_id: `batch-${inspectedBatches.length}`,
        status: completed ? "completed" : "queued",
        submitted_count: body.resource_ids.length,
        completed_count: completed ? body.resource_ids.length : 0,
        results: completed ? body.resource_ids.map((resourceId, index) => inspectionResult(resourceId, "verified", index)) : [],
      },
    });
  });
  const inspectionResult = (resourceId: string, status: "verified" | "unsupported" | "timeout" | "failed", index: number) => ({
    resource_id: resourceId,
    infohash: status === "unsupported" ? null : `hash-${resourceId}`,
    status,
    total_size_bytes: status === "verified" ? (index === 0 ? 1073741824 : 900000000 + index) : 0,
    file_count: status === "verified" ? 3 : 0,
    video_file_count: status === "verified" ? 1 : 0,
    video_size_bytes: status === "verified" ? 1000000000 : 0,
    subtitle_count: status === "verified" ? 2 : 0,
    sample_count: status === "verified" ? 1 : 0,
    largest_video_name: status === "verified" ? "episode.mkv" : null,
    content_summary: status === "verified" ? "verified" : null,
    error_code: status === "verified" ? null : status.toUpperCase(),
  });
  await page.route("**/api/v1/resources/inspect/*", (route) => {
    inspectGetCount += 1;
    const batchIds = inspectedBatches[0] ?? [];
    const results = inspectGetCount === 1
      ? batchIds.slice(0, 2).map((resourceId, index) => inspectionResult(resourceId, "verified", index))
      : batchIds.map((resourceId, index) => inspectionResult(resourceId, "verified", index));
    return route.fulfill({
      json: {
        batch_id: "batch-1",
        status: inspectGetCount === 1 ? "running" : "completed",
        submitted_count: batchIds.length,
        completed_count: inspectGetCount === 1 ? 2 : batchIds.length,
        results,
      },
    });
  });

  await page.goto("/tv/1399?season=2");
  await expect(page.locator("#season-select")).toHaveValue("2");
  await expect(page.locator(".resource-table tbody tr")).toHaveCount(31);
  const shareResource = testInfo.project.name.startsWith("mobile")
    ? page.locator(".resource-cards").getByText("115 分享资源")
    : page.locator(".resource-table").getByText("115 分享资源");
  await expect(shareResource).toBeVisible();
  await expect.poll(() => inspectedBatches.length).toBe(1);
  expect(inspectedBatches[0]).toHaveLength(8);
  expect(inspectedBatches[0].every((id) => id.startsWith("magnet-"))).toBe(true);
  await expect(page.getByLabel("资源名称搜索")).toBeVisible();
  await expect(page.getByRole("group", { name: "资源名称标签" }).getByRole("button", { name: /4K\/2160P/ })).toContainText("1");
  await expect(page.getByRole("group", { name: "资源名称标签" }).getByRole("button", { name: "字幕 1" })).toBeVisible();
  await page.getByLabel("资源名称搜索").fill("2160P");
  await expect(page.locator(".resource-table tbody tr")).toHaveCount(1);
  await page.getByLabel("资源名称搜索").fill("");
  await page.getByRole("group", { name: "资源名称标签" }).getByRole("button", { name: /全部/ }).click();
  await page.locator('select[aria-label="资源排序"]').selectOption("seeders");
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("watch-assistant:resource-sort"))).toBe("seeders");
  await page.locator('select[aria-label="资源排序"]').selectOption("comprehensive");
  await expect(page.getByRole("status")).toContainText("8 / 8");
  await expect(page.locator(".resource-table tbody tr").first()).toContainText("已验证");
  await expect(page.locator(".resource-table tbody tr").first()).toContainText("已验证");
  await expect(page.getByRole("button", { name: "检测更多" })).toBeVisible();
  expect(await page.evaluate(() => {
    const boxes = [...document.querySelectorAll<HTMLElement>(".resource-controls > *")]
      .filter((element) => getComputedStyle(element).display !== "none")
      .map((element) => element.getBoundingClientRect());
    return boxes.every((box, index) => boxes.slice(index + 1).every((other) => box.right <= other.left + 1 || other.right <= box.left + 1 || box.bottom <= other.top + 1 || other.bottom <= box.top + 1));
  })).toBe(true);
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);

  await page.screenshot({ path: testInfo.outputPath("season-quality.png"), fullPage: true });
  await page.getByRole("button", { name: "检测更多" }).click();
  await expect.poll(() => inspectedBatches.length).toBe(2);
  expect(inspectedBatches[1]).toHaveLength(8);
  expect(inspectedBatches[1].every((id) => !inspectedBatches[0].includes(id))).toBe(true);
  await expect(page.getByRole("status")).toContainText("8 / 8");

  await page.goto("/tv/1399?season=0");
  await expect(page.locator("#season-select")).toHaveValue("0");
  expect(searchRequests.at(-1)?.season_number).toBe(0);
  await expect.poll(() => inspectedBatches.length).toBe(3);
  expect(inspectGetCount).toBe(2);

  await page.goto("/movie/27205?season=2");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page.locator("#season-select")).toHaveCount(0);
  expect(searchRequests.at(-1)?.season_number).toBeUndefined();
  expect(inspectedBatches.length).toBe(3);

  delayNextSearch = true;
  await page.getByRole("button", { name: "刷新资源" }).click();
  await page.getByRole("button", { name: "电影", exact: true }).click();
  await expect(page.getByRole("heading", { name: "电影库" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "权力的游戏" })).toHaveCount(0);
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth)).toBe(true);
});

test("does not start inspection when the backend disables it", async ({ page }) => {
  let inspectPostCount = 0;
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => route.fulfill({
    json: { movie, results: [{ resource_id: "magnet-1", kind: "magnet", name: "资源", size_bytes: null, seeders: null, source: "test", captured_at: "2026-07-24T10:00:00Z" }], warnings: [], cached: false, cache_age_seconds: null },
  }));
  await page.route("**/api/v1/resources/inspect", (route) => {
    inspectPostCount += 1;
    return route.fulfill({ status: 500, json: { detail: "must not be called" } });
  });

  await page.goto("/movie/27205");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await page.waitForTimeout(100);
  expect(inspectPostCount).toBe(0);
  await expect(page.getByRole("button", { name: "检测更多" })).toHaveCount(0);
});

test("invalidates an automatic batch when switching seasons quickly", async ({ page }) => {
  let inspectPostCount = 0;
  const seasons = [
    { season_number: 1, name: "第 1 季", episode_count: 10, air_date: "2011-04-17", poster_path: null },
    { season_number: 2, name: "第 2 季", episode_count: 10, air_date: "2012-04-01", poster_path: null },
  ];
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => {
    const request = route.request().postDataJSON() as { season_number?: number };
    const season = request.season_number ?? 1;
    return route.fulfill({
      json: {
        movie: { ...show, seasons },
        results: [{ resource_id: `season-${season}`, kind: "magnet", name: `第 ${season} 季资源`, size_bytes: null, seeders: null, source: "test", captured_at: "2026-07-24T10:00:00Z" }],
        warnings: [],
        cached: false,
        cache_age_seconds: null,
        selected_season: season,
      },
    });
  });
  await page.route("**/api/v1/resources/inspect", async (route) => {
    inspectPostCount += 1;
    if (inspectPostCount === 1) await new Promise((resolve) => setTimeout(resolve, 250));
    const resourceId = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids[0];
    return route.fulfill({
      json: {
        batch_id: `batch-${inspectPostCount}`,
        status: "completed",
        submitted_count: 1,
        completed_count: 1,
        results: [{ resource_id: resourceId, infohash: "hash", status: "verified", total_size_bytes: 1024, file_count: 1, video_file_count: 1, video_size_bytes: 1024, subtitle_count: 0, sample_count: 0, largest_video_name: "episode.mkv", content_summary: null, error_code: null }],
      },
    });
  });

  await page.goto("/tv/1399?season=2");
  await expect.poll(() => inspectPostCount).toBe(1);
  await page.goto("/tv/1399?season=1");
  await expect(page.locator("#season-select")).toHaveValue("1");
  await expect(page.locator(".resource-name:visible, .resource-card h3:visible").filter({ hasText: "第 1 季资源" })).toBeVisible();
  await expect.poll(() => inspectPostCount).toBe(2);
  await expect(page.getByRole("status")).toContainText("1 / 1");
  await page.waitForTimeout(350);
  await expect(page.locator(".resource-name:visible, .resource-card h3:visible").filter({ hasText: "第 2 季资源" })).toHaveCount(0);
  await expect(page.locator(".resource-name:visible, .resource-card h3:visible").filter({ hasText: "第 1 季资源" })).toBeVisible();
});

test("exposes a retry action after POST failure and resubmits the same resource once", async ({ page }) => {
  const submittedIds: string[][] = [];
  let inspectPostCount = 0;
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => route.fulfill({
    json: {
      movie,
      results: [{ resource_id: "retry-me", kind: "magnet", name: "Retry Me 1080P", size_bytes: null, seeders: null, source: "test", captured_at: "2026-07-24T10:00:00Z" }],
      warnings: [],
      cached: false,
      cache_age_seconds: null,
    },
  }));
  await page.route("**/api/v1/resources/inspect", async (route) => {
    inspectPostCount += 1;
    const resourceIds = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    submittedIds.push(resourceIds);
    if (inspectPostCount === 1) {
      return route.fulfill({ status: 500, json: { detail: "inspection unavailable" } });
    }
    return route.fulfill({
      json: {
        batch_id: "retry-batch",
        status: "queued",
        submitted_count: 1,
        completed_count: 0,
        results: [],
      },
    });
  });
  await page.route("**/api/v1/resources/inspect/*", (route) => route.fulfill({
    json: {
      batch_id: "retry-batch",
      status: "completed",
      submitted_count: 1,
      completed_count: 1,
      results: [inspectionResult("retry-me", "verified")],
    },
  }));

  await page.goto("/movie/27205");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  await expect(page.locator(".resource-table:visible .inspection-cell strong, .resource-cards:visible .inspection-details").filter({ hasText: "检测失败" })).toBeVisible();

  await page.getByRole("button", { name: "重试失败项" }).click();
  await expect.poll(() => inspectPostCount).toBe(2);
  expect(submittedIds).toEqual([["retry-me"], ["retry-me"]]);
  await expect(page.getByRole("button", { name: "重试失败项" })).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("1 / 1");
});

test("keeps more and retry actions separate when eight failures and untested resources coexist", async ({ page }) => {
  const submittedIds: string[][] = [];
  let inspectPostCount = 0;
  const resources = Array.from({ length: 12 }, (_, index) => ({
    resource_id: `resource-${index}`,
    kind: "magnet",
    name: `Resource ${index}`,
    size_bytes: null,
    seeders: null,
    source: "test",
    captured_at: "2026-07-24T10:00:00Z",
  }));
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => route.fulfill({
    json: { movie, results: resources, warnings: [], cached: false, cache_age_seconds: null },
  }));
  await page.route("**/api/v1/resources/inspect", async (route) => {
    const resourceIds = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    submittedIds.push(resourceIds);
    inspectPostCount += 1;
    const status: "failed" | "verified" = inspectPostCount === 1 ? "failed" : "verified";
    return route.fulfill({
      json: {
        batch_id: `partial-batch-${inspectPostCount}`,
        status: inspectPostCount === 1 ? "partial" : "completed",
        submitted_count: resourceIds.length,
        completed_count: resourceIds.length,
        results: resourceIds.map((resourceId) => inspectionResult(resourceId, status)),
      },
    });
  });

  await page.goto("/movie/27205");
  await expect(page.getByRole("button", { name: "检测更多" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  expect(await page.evaluate(() => {
    const buttons = [...document.querySelectorAll<HTMLElement>(".resource-controls .inspection-more-button")]
      .filter((button) => getComputedStyle(button).display !== "none");
    const boxes = buttons.map((button) => button.getBoundingClientRect());
    return boxes.every((box, index) => boxes.slice(index + 1).every((other) =>
      box.right <= other.left + 1
      || other.right <= box.left + 1
      || box.bottom <= other.top + 1
      || other.bottom <= box.top + 1,
    )) && buttons.every((button) => button.scrollWidth <= button.clientWidth)
      && document.body.scrollWidth <= window.innerWidth;
  })).toBe(true);
  await page.getByRole("button", { name: "检测更多" }).click();
  await expect.poll(() => inspectPostCount).toBe(2);
  expect(submittedIds).toEqual([
    resources.slice(0, 8).map((resource) => resource.resource_id),
    resources.slice(8).map((resource) => resource.resource_id),
  ]);

  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  await page.getByRole("button", { name: "重试失败项" }).click();
  await expect.poll(() => inspectPostCount).toBe(3);
  expect(submittedIds[2]).toEqual(resources.slice(0, 8).map((resource) => resource.resource_id));
  expect(new Set(submittedIds.flat()).size).toBe(12);
  await expect(page.getByRole("button", { name: "检测更多" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重试失败项" })).toHaveCount(0);
});

test("clears the failed inspection queue when switching seasons", async ({ page }) => {
  const submittedIds: string[][] = [];
  let inspectPostCount = 0;
  const seasons = [
    { season_number: 1, name: "第 1 季", episode_count: 8, air_date: "2011-01-01", poster_path: null },
    { season_number: 2, name: "第 2 季", episode_count: 8, air_date: "2012-01-01", poster_path: null },
  ];
  const seasonOneResources = Array.from({ length: 8 }, (_, index) => ({
    resource_id: `season-one-${index}`,
    kind: "magnet",
    name: `第 1 季资源 ${index}`,
    size_bytes: null,
    seeders: null,
    source: "test",
    captured_at: "2026-07-24T10:00:00Z",
  }));
  seasonOneResources.push({
    resource_id: "legacy-failed",
    kind: "magnet",
    name: "旧失败资源",
    size_bytes: null,
    seeders: null,
    source: "test",
    captured_at: "2026-07-24T10:00:00Z",
  });
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => {
    const season = Number((route.request().postDataJSON() as { season_number?: number }).season_number ?? 2);
    return route.fulfill({
      json: {
        movie: { ...show, seasons },
        results: season === 1 ? seasonOneResources : [{ ...seasonOneResources[8], name: "第 2 季失败资源" }],
        warnings: [],
        cached: false,
        cache_age_seconds: null,
        selected_season: season,
      },
    });
  });
  await page.route("**/api/v1/resources/inspect", async (route) => {
    const resourceIds = (route.request().postDataJSON() as { resource_ids: string[] }).resource_ids;
    submittedIds.push(resourceIds);
    inspectPostCount += 1;
    if (inspectPostCount === 1) return route.fulfill({ status: 500, json: { detail: "failed" } });
    return route.fulfill({
      json: {
        batch_id: `season-batch-${inspectPostCount}`,
        status: "completed",
        submitted_count: resourceIds.length,
        completed_count: resourceIds.length,
        results: resourceIds.map((resourceId) => inspectionResult(resourceId, "verified")),
      },
    });
  });

  await page.goto("/tv/1399?season=2");
  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  await page.goto("/tv/1399?season=1");
  await expect(page.locator("#season-select")).toHaveValue("1");
  await expect.poll(() => inspectPostCount).toBe(2);
  expect(submittedIds[1]).toEqual(seasonOneResources.slice(0, 8).map((resource) => resource.resource_id));
  await expect(page.getByRole("button", { name: "检测更多" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试失败项" })).toHaveCount(0);
});
