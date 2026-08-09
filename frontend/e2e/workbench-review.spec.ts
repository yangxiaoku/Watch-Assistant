import { expect, test } from "@playwright/test";

const movie = {
  tmdb_id: 27205,
  media_type: "movie" as const,
  title: "测试电影",
  original_title: "Test Movie",
  release_year: 2026,
  overview: "测试简介",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [],
  vote_average: 7.5,
};

const resourceSearch = {
  task_id: "resource-search-review",
  tmdb_id: movie.tmdb_id,
  media_type: movie.media_type,
  season_number: null,
  status: "ready" as const,
  snapshot_revision: "snapshot-review",
  query_plan_version: "v1",
  selected_season: null,
  sources: [],
  warnings: [],
  cache_age_seconds: null,
  error_code: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

function resourcePage(name: string, revision: string) {
  return {
    items: [{
      resource_id: revision,
      kind: "magnet" as const,
      name,
      size_bytes: null,
      seeders: null,
      source: "test",
      captured_at: "2026-08-01T00:00:00Z",
    }],
    page: 1,
    page_size: 25 as const,
    total: 1,
    total_pages: 1,
    facets: { magnet: 1, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 },
    snapshot_revision: revision,
  };
}

function visibleResource(page: import("@playwright/test").Page, name: string) {
  return page.locator(".resource-title:visible, .resource-card-title:visible").filter({ hasText: name });
}

async function installDetailRoutes(page: import("@playwright/test").Page, metadataHandler: (route: import("@playwright/test").Route) => Promise<void> | void) {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/media/movie/27205", metadataHandler);
  await page.route("**/api/v1/media/movie/27205/performance", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/v1/media/movie/27205/resource-search", (route) => route.fulfill({ json: resourceSearch }));
}

test("retries metadata against the correct media endpoint", async ({ page }) => {
  let metadataAttempts = 0;
  await installDetailRoutes(page, (route) => {
    metadataAttempts += 1;
    if (metadataAttempts === 1) return route.fulfill({ status: 503, json: { detail: { code: "tmdb_unavailable" } } });
    return route.fulfill({ json: movie });
  });
  await page.route("**/api/v1/media/movie/27205/resources?**", (route) => route.fulfill({ json: resourcePage("资源占位", "snapshot-metadata") }));

  await page.goto("/movie/27205");
  await expect(page.getByRole("button", { name: "重试资料" })).toBeVisible();
  await page.getByRole("button", { name: "重试资料" }).click();
  await expect.poll(() => metadataAttempts).toBe(2);
  await expect(page.getByRole("heading", { name: "测试电影" })).toBeVisible();
});

test("keeps the previous resource row while refresh pagination is pending", async ({ page }) => {
  await installDetailRoutes(page, (route) => route.fulfill({ json: movie }));
  let resourcePageCalls = 0;
  let releaseRefreshPage: (() => void) | null = null;
  await page.route("**/api/v1/media/movie/27205/resources?**", async (route) => {
    resourcePageCalls += 1;
    if (resourcePageCalls === 2) await new Promise<void>((resolve) => { releaseRefreshPage = resolve; });
    await route.fulfill({ json: resourcePage(resourcePageCalls === 1 ? "旧资源" : "新资源", resourcePageCalls === 1 ? "snapshot-old" : "snapshot-new") });
  });

  await page.goto("/movie/27205");
  await expect(visibleResource(page, "旧资源")).toBeVisible();
  await page.getByRole("button", { name: "刷新资源" }).click();
  await expect.poll(() => resourcePageCalls).toBe(2);
  await expect(visibleResource(page, "旧资源")).toBeVisible();
  await expect(visibleResource(page, "新资源")).toHaveCount(0);

  releaseRefreshPage?.();
  await expect(visibleResource(page, "新资源")).toBeVisible();
});

const mixedWorkflow = {
  id: "workflow-mixed-review",
  correlation_id: "correlation-mixed-review",
  media_type: "movie" as const,
  tmdb_id: 27205,
  subscription_id: null,
  status: "in_progress" as const,
  status_zh: "处理中",
  state_reason: "stage_in_progress",
  state_reason_zh: "当前阶段处理中。",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:01:00Z",
  stages: [
    {
      id: "stage-running-review",
      stage: "push" as const,
      status: "running" as const,
      status_zh: "处理中",
      reason: null,
      reason_zh: null,
      error_code: null,
      child_type: "task",
      child_id: "task-running-review",
      started_at: "2026-08-01T00:00:00Z",
      completed_at: null,
      updated_at: "2026-08-01T00:01:00Z",
    },
    {
      id: "stage-pending-review",
      stage: "availability" as const,
      status: "pending" as const,
      status_zh: "待开始",
      reason: null,
      reason_zh: null,
      error_code: null,
      child_type: null,
      child_id: null,
      started_at: null,
      completed_at: null,
      updated_at: "2026-08-01T00:01:00Z",
    },
  ],
};

test("shows cancellation for pending stages without hiding running work", async ({ page }) => {
  let cancelCalls = 0;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/workflows?**", (route) => route.fulfill({ json: { items: [mixedWorkflow], page: 1, page_size: 20, total: 1 } }));
  await page.route(`**/api/v1/workflows/${mixedWorkflow.id}`, (route) => route.fulfill({ json: mixedWorkflow }));
  await page.route(`**/api/v1/workflows/${mixedWorkflow.id}/cancel`, async (route) => {
    cancelCalls += 1;
    await route.fulfill({ json: { ...mixedWorkflow, status: "partial", status_zh: "部分完成" } });
  });

  await page.goto("/workflows");
  await page.locator(".workflow-row").click();
  await expect(page.getByRole("button", { name: "取消工作流" })).toBeVisible();
  await page.getByRole("button", { name: "取消工作流" }).click();
  await expect(page.getByRole("dialog")).toContainText("运行中或结果待确认的远端操作不会被伪造撤回");
  await expect(page.getByRole("dialog").getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "确认取消" }).click();
  await expect.poll(() => cancelCalls).toBe(1);
});
