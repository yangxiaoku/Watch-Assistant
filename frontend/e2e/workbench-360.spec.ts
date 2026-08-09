import { expect, test } from "@playwright/test";

const workflow = {
  id: "workflow-private-360",
  correlation_id: "correlation-private-360",
  media_type: "movie" as const,
  tmdb_id: 27205,
  subscription_id: null,
  status: "result_pending_confirmation" as const,
  status_zh: "结果待确认",
  state_reason: "uncertain_requires_verification",
  state_reason_zh: "先核对远端结果，确认前不要重复操作。",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:01:00Z",
  stages: [{
    id: "stage-private-360",
    stage: "push" as const,
    status: "uncertain" as const,
    status_zh: "结果待确认",
    reason: "uncertain_requires_verification",
    reason_zh: "远端结果需要只读核对。",
    error_code: "uncertain_requires_verification",
    child_type: "task",
    child_id: "task-private-360",
    started_at: "2026-08-01T00:00:00Z",
    completed_at: null,
    updated_at: "2026-08-01T00:01:00Z",
  }],
};

const task = {
  id: "task-private-360",
  resource_id: "resource-private-360",
  workflow_id: workflow.id,
  target_directory_id: null,
  action: "offline_download" as const,
  state: "failed" as const,
  attempts: 1,
  remote_ref: null,
  error_code: "internal_error",
  error_message: "raw backend exception must stay hidden",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:01:00Z",
  submitted_at: null,
  state_zh: "处理失败",
  state_reason_zh: null,
};

test("keeps task and workflow workbench usable at 360px", async ({ page }) => {
  let taskState: "failed" | "queued" | "cancelled" = task.state;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/workflows?**", (route) => route.fulfill({ json: { items: [workflow], page: 1, page_size: 20, total: 1 } }));
  await page.route(`**/api/v1/workflows/${workflow.id}`, (route) => route.fulfill({ json: workflow }));
  await page.route("**/api/v1/tasks", (route) => route.fulfill({ json: [{ ...task, state: taskState }] }));
  await page.route(`**/api/v1/tasks/${task.id}/retry`, (route) => {
    taskState = "queued";
    return route.fulfill({ json: { ...task, state: taskState, state_zh: "排队中", error_code: null, error_message: null } });
  });
  await page.route(`**/api/v1/tasks/${task.id}/cancel`, (route) => {
    taskState = "cancelled";
    return route.fulfill({ json: { ...task, state: taskState, state_zh: "已取消", error_code: null, error_message: null } });
  });
  await page.route(`**/api/v1/tasks/${task.id}`, (route) => route.fulfill({ json: { ...task, state: taskState, state_zh: taskState === "queued" ? "排队中" : task.state_zh } }));

  await page.goto("/workflows");
  await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible();
  await page.locator(".workflow-row").first().click();
  await expect(page.locator(".workflow-detail").getByText("结果待确认").first()).toBeVisible();
  await expect(page.getByText(workflow.id, { exact: true })).toBeHidden();
  await expect(page.locator(".workflow-diagnostics")).toHaveCount(1);

  await page.getByRole("button", { name: "推送任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "推送任务" })).toBeVisible();
  await expect(page.getByText("raw backend exception must stay hidden", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "重新排队" }).click();
  await expect(page.getByRole("button", { name: "取消排队" })).toBeVisible();
  await page.getByRole("button", { name: "取消排队" }).click();
  await expect(page.getByRole("dialog").getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "确认取消" }).click();
  await expect(page.locator(".task-drawer").getByText("已取消").first()).toBeVisible();

  const layout = await page.evaluate(() => {
    const visible = Array.from(document.querySelectorAll<HTMLElement>("body *")).filter((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    });
    const overflow = visible.filter((element) => {
      if (element.closest(".primary-nav")) return false;
      const rect = element.getBoundingClientRect();
      return rect.left < -1 || rect.right > window.innerWidth + 1;
    }).slice(0, 5).map((element) => element.className || element.tagName);
    const title = document.querySelector<HTMLElement>(".task-drawer h2")?.getBoundingClientRect();
    const actions = document.querySelector<HTMLElement>(".task-drawer-header-actions")?.getBoundingClientRect();
    return {
      width: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      overflow,
      headerOverlaps: Boolean(title && actions && title.right > actions.left),
    };
  });
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.width);
  expect(layout.overflow).toEqual([]);
  expect(layout.headerOverlaps).toBe(false);
});

test("keeps the 115 settings heading readable at 360px", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-360", "此专项只在 360px 项目执行");
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/settings/**", (route) => route.fulfill({ status: 404, json: { detail: "not_found" } }));
  await page.route("**/api/v1/logs?**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.route("**/api/v1/settings/organization", (route) => route.fulfill({ json: {
    revision: 0,
    schedule_enabled: false,
    scan_interval_minutes: 60,
    source_directory_ids: [],
    source_directory_labels: [],
    target_directory_id: null,
    target_directory_label: null,
    push_directory_id: null,
    push_directory_label: null,
    video_extensions: ["mkv"],
    metadata_extensions: ["srt"],
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

  await page.goto("/workbench");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await page.locator(".settings-mobile-select select").selectOption("organization");
  await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible();

  const layout = await page.evaluate(() => {
    const copy = document.querySelector<HTMLElement>(".settings-section-heading > div:first-child");
    const actions = document.querySelector<HTMLElement>(".settings-section-actions");
    const section = document.querySelector<HTMLElement>(".settings-section");
    if (!copy || !actions || !section) throw new Error("missing organization settings heading");
    const copyBox = copy.getBoundingClientRect();
    const actionBox = actions.getBoundingClientRect();
    return {
      copyWidth: copyBox.width,
      copyBottom: copyBox.bottom,
      actionsTop: actionBox.top,
      sectionScrollWidth: section.scrollWidth,
      sectionClientWidth: section.clientWidth,
    };
  });
  expect(layout.copyWidth).toBeGreaterThan(200);
  expect(layout.copyBottom).toBeLessThanOrEqual(layout.actionsTop);
  expect(layout.sectionScrollWidth).toBeLessThanOrEqual(layout.sectionClientWidth);
  await page.screenshot({ path: testInfo.outputPath("settings-360.png"), fullPage: true });
});
