import { expect, test } from "@playwright/test";

const needsReview = {
  plan_id: "plan-local-1",
  plan_hash: "a".repeat(64),
  status: "needs_review",
  revision: 4,
  expires_at: "2026-08-01T00:00:00Z",
  source_count: 2,
  action_count: 1,
  precondition_count: 1,
  executable_action_count: 1,
  review_action_count: 0,
  can_execute: true,
  execution_blockers: [],
  alias: null,
};
const reviewOnly = {
  ...needsReview,
  source_count: 1,
  action_count: 1,
  executable_action_count: 0,
  review_action_count: 1,
  can_execute: false,
  execution_blockers: [{
    kind: "review_only",
    code: "plan_has_review_actions",
    message_zh: "计划中仍有不能自动执行的动作。",
    next_step_zh: "逐项复核计划内容，处理待确认动作后再执行。",
  }],
  candidates: [],
};
let activePlan = needsReview;
let confirmationCalls = 0;

test.beforeEach(async ({ page }) => {
  confirmationCalls = 0;
  activePlan = needsReview;
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, organization_plan_enabled: true, organization_execution_enabled: true, organization_execution_supported: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-local" } }),
  );
  await page.route("**/api/v1/organization-plans**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith("/operation") && request.method() === "GET") {
      await route.fulfill({ status: 404, json: { detail: { code: "operation_not_found", message: "整理操作不存在" } } });
      return;
    }
    if (pathname.endsWith("/candidate-search") && request.method() === "POST") {
      activePlan = {
        ...activePlan,
        revision: activePlan.revision + 1,
        candidates: [{ source_object_id: "source-no-candidate", tmdb_id: 42, title: "The Office", media_type: "movie", release_year: 2005 }],
      };
      await route.fulfill({ json: activePlan });
      return;
    }
    if (pathname.endsWith("/candidate") && request.method() === "POST") {
      activePlan = {
        ...activePlan,
        status: "planned",
        revision: activePlan.revision + 1,
        candidates: [],
        executable_action_count: 1,
        review_action_count: 0,
        can_execute: true,
        execution_blockers: [],
      };
      await route.fulfill({ json: activePlan });
      return;
    }
    if (pathname.endsWith("/operation") && request.method() === "POST") {
      confirmationCalls += 1;
      await route.fulfill({ json: { operation_id: "op-confirm", plan_id: activePlan.plan_id, status: "organized", revision: 1, attempts: 1, error_code: null, cancel_requested: false } });
      return;
    }
    if (pathname.endsWith("/confirm-and-operation") && request.method() === "POST") {
      confirmationCalls += 1;
      await route.fulfill({ json: { operation_id: "op-confirm", plan_id: needsReview.plan_id, status: "organized", revision: 1, attempts: 1, error_code: null, cancel_requested: false } });
      return;
    }
    await route.fulfill({ json: { items: [activePlan], next_cursor: null } });
  });
});

test("reviews a local plan on desktop and mobile without exposing remote data", async ({ page }) => {
  await page.goto("/organization");
  await page.getByRole("button", { name: "待处理" }).click();
  await expect(page.getByText("计划 plan-loc", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("remote-private", { exact: false })).toHaveCount(0);
  await expect(page.getByText("pickcode", { exact: false })).toHaveCount(0);

  await page.getByRole("button", { name: "确认并开始整理" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /确认并/ }).click();
  await expect(page.getByText("整理已完成")).toBeVisible();
  expect(confirmationCalls).toBe(1);

  const viewport = await page.evaluate(() => ({ width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width);
});

test("unmatched review plan searches, selects, previews a move, then confirms once", async ({ page }) => {
  activePlan = reviewOnly;
  await page.goto("/organization");
  await page.getByRole("button", { name: "待处理" }).click();

  await expect(page.getByText("可执行移动").locator(".." )).toContainText("0");
  await expect(page.getByText("计划中仍有不能自动执行的动作。")).toBeVisible();
  await expect(page.getByText("下一步：逐项复核计划内容，处理待确认动作后再执行。")).toBeVisible();
  await expect(page.getByRole("button", { name: "确认并开始整理" })).toHaveCount(0);
  await page.getByPlaceholder("输入片名或年份").fill("The Office 2005");
  await page.getByRole("button", { name: "搜索候选" }).click();
  await expect(page.getByRole("button", { name: /The Office/ })).toBeVisible();

  await page.getByRole("button", { name: /The Office/ }).click();
  await expect(page.getByText("可执行移动").locator(".." )).toContainText("1");
  await expect(page.getByText("待复核动作").locator(".." )).toContainText("0");
  await page.getByRole("button", { name: "立即整理" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /确认并/ }).click();
  await expect(page.getByText("整理已完成")).toBeVisible();
  expect(confirmationCalls).toBe(1);
});

test("shows distinct tab headings for review and history", async ({ page }) => {
  await page.route("**/api/v1/settings/organization", (route) => route.fulfill({ json: {
    revision: 0,
    schedule_enabled: false,
    auto_execute_enabled: true,
    scan_interval_minutes: 60,
    source_directory_ids: [],
    source_directory_labels: [],
    target_directory_id: null,
    target_directory_label: null,
    push_directory_id: null,
    push_directory_label: null,
    video_extensions: [],
    metadata_extensions: [],
    rename_enabled: false,
    media_probe_enabled: false,
    ai_identification_enabled: false,
    small_file_threshold_mb: 0,
    cleanup_empty_directories: false,
    strm_linkage_enabled: false,
    operation_delay_seconds: 0,
    include_children_category: false,
    include_concert_category: false,
    region_grouping_enabled: false,
    year_grouping_enabled: false,
    prefer_remux: false,
    prefer_resolution: false,
    prefer_dolby: false,
    conflict_mode: 2,
    multi_version_enabled: false,
  } }));
  await page.route("**/api/v1/settings/organization/result", (route) => route.fulfill({ json: {
    status: "unknown",
    available_statuses: ["unknown", "success", "skipped", "deleted", "replace", "failed"],
    source_count: 0,
    scanned_count: 0,
    plan_count: 0,
    queued_count: 0,
    blocked_count: 0,
    blocked_details: [],
    items: [],
    finished_at: null,
    run_id: null,
  } }));
  await page.route("**/api/v1/organization-history**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));

  await page.goto("/organization");
  await page.getByRole("button", { name: "待处理" }).click();
  await expect(page.getByRole("heading", { name: "待处理计划" })).toBeVisible();
  await page.getByRole("button", { name: "历史" }).click();
  await expect(page.getByRole("heading", { name: "整理历史" })).toBeVisible();
});
