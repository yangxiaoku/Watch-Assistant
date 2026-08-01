import { expect, test, type Page } from "@playwright/test";

const library = {
  library_id: "main",
  name: "115 媒体库",
  root_directory_id: "1000",
  enabled: true,
  scope_verified: true,
  revision: 2,
  latest_scan: {
    run_id: "scan-1",
    state: "completed",
    complete: true,
    snapshot_revision: 1,
    pages_read: 2,
    items_seen: 1,
    added_count: 1,
    changed_count: 0,
    removed_count: 0,
    error_code: null,
  },
};

const operation = {
  operation_id: "strm_op_1",
  library_id: "main",
  source_scan_run_id: "scan-1",
  workflow_id: null,
  kind: "full",
  status: "succeeded",
  generated: 1,
  unchanged: 0,
  skipped: 0,
  failed: 0,
  retired: 0,
  error_code: null,
  created_at: "2026-08-01T00:00:00Z",
  started_at: "2026-08-01T00:00:00Z",
  finished_at: "2026-08-01T00:00:01Z",
};

const cleanupPlan = {
  plan_id: "strm_cleanup_plan_1",
  library_id: "main",
  source_scan_run_id: "scan-1",
  source_snapshot_revision: 1,
  plan_hash: "a".repeat(64),
  status: "needs_review",
  revision: 1,
  expires_at: "2026-08-01T00:00:00Z",
  candidate_count: 1,
  executable_count: 1,
  blocked_count: 0,
};

test("runs full STRM generation and reviewed cleanup on the library workbench", async ({ page }, testInfo) => {
  await page.on("dialog", (dialog) => dialog.accept());
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false, strm_capabilities: { full: true, incremental: true, cleanup: true, playback: false, playback_contract_verified: false } } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/libraries?**", (route) => route.fulfill({ json: { items: [library], next_cursor: null } }));
  await page.route("**/api/v1/libraries/main/media**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.route("**/api/v1/libraries/main/strm-manifest**", (route) => route.fulfill({ json: { items: [], page: 1, page_size: 50, total: 0, total_pages: 0 } }));
  await page.route("**/api/v1/libraries/main/strm-operations**", (route) => route.fulfill({ json: { items: [operation], next_cursor: null } }));
  await page.route("**/api/v1/strm-operations/strm_op_1", (route) => route.fulfill({ json: operation }));
  await page.route("**/api/v1/libraries/main/strm-generation", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ source_scan_run_id: "scan-1" });
    await route.fulfill({ json: { operation_id: "strm_op_1", library_id: "main", scan_run_id: "scan-1", generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0 } });
  });
  await page.route("**/api/v1/libraries/main/strm-cleanup-plan", async (route) => {
    expect(route.request().method()).toBe("POST");
    await route.fulfill({ json: cleanupPlan });
  });
  await page.route("**/api/v1/strm-cleanup-plans/strm_cleanup_plan_1/apply", async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    expect(body.expected_revision).toBe(1);
    expect(body.digest).toBe("a".repeat(64));
    expect(body.confirm).toBe(true);
    expect(typeof body.idempotency_key).toBe("string");
    await route.fulfill({ json: { plan: { ...cleanupPlan, status: "applied", revision: 2 }, retired: 1 } });
  });

  await page.goto("/library");
  await expect(page.getByRole("heading", { name: "媒体库与 STRM" })).toBeVisible();
  await page.getByRole("button", { name: "全量 STRM" }).click();
  await expect(page.getByText("STRM 全量同步完成")).toBeVisible();
  await expect(page.locator(".library-operation-section strong")).toHaveText("已完成");

  await page.getByRole("button", { name: "预览失效清理" }).click();
  await expect(page.getByText("失效清理预览已生成，共 1 项，1 项可执行")).toBeVisible();
  await page.getByRole("button", { name: "确认执行清理" }).click();
  await expect(page.getByText("失效清理已完成，退休 1 个受管 STRM")).toBeVisible();
  await expect(page.locator(".library-operation-section strong")).toHaveText("已完成");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`library-strm-${testInfo.project.name}.png`), fullPage: true });
});

async function routeLibraryFixture(page: Page, health: object) {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: health }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-capability" } }));
  await page.route("**/api/v1/settings/overview", (route) => route.fulfill({ json: {
    release: "fixture",
    uptime_seconds: 1,
    database_size_bytes: 1,
    capabilities: {
      inspection: false,
      magnet: false,
      share: false,
      organization_plan: false,
      organization_execution: false,
      organization_write: false,
      permanent_delete: false,
      strm_full: false,
      strm_incremental: false,
      strm_cleanup: false,
      strm_playback: false,
      organization_empty_directory_cleanup: false,
    },
    capability_statuses: {},
    capability_details: {},
  } }));
  await page.route("**/api/v1/libraries?**", (route) => route.fulfill({ json: { items: [library], next_cursor: null } }));
  await page.route("**/api/v1/libraries/main/media**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.route("**/api/v1/libraries/main/strm-manifest**", (route) => route.fulfill({ json: { items: [], page: 1, page_size: 50, total: 0, total_pages: 0 } }));
  await page.route("**/api/v1/libraries/main/strm-operations**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
}

test("blocks STRM cleanup before a disabled capability can reach the API", async ({ page }) => {
  const reason = "STRM 失效清理未启用，请检查部署功能开关。";
  await routeLibraryFixture(page, {
    status: "ok",
    push_supported: false,
    strm_capabilities: {
      full: true,
      incremental: true,
      cleanup: false,
      cleanup_capability: { enabled: false, reason_code: "strm_cleanup_disabled", reason_zh: reason, settings_section: "overview" },
      playback: false,
      playback_contract_verified: false,
    },
    organization_capabilities: {
      empty_directory_cleanup: { enabled: true, reason_code: null, reason_zh: "可用", settings_section: "organization" },
    },
  });
  await page.goto("/library");
  await expect(page.getByRole("button", { name: "预览失效清理" })).toBeDisabled();
  await expect(page.getByText(reason)).toBeVisible();
  await page.getByRole("button", { name: "前往设置" }).click();
  await expect(page).toHaveURL(/\/settings$/);
});

test("blocks empty-directory cleanup independently from STRM cleanup", async ({ page }) => {
  const reason = "空目录回收未就绪，请前往自动整理设置检查开关和写入契约。";
  await routeLibraryFixture(page, {
    status: "ok",
    push_supported: false,
    strm_capabilities: {
      full: true,
      incremental: true,
      cleanup: true,
      cleanup_capability: { enabled: true, reason_code: null, reason_zh: "可用", settings_section: "overview" },
      playback: false,
      playback_contract_verified: false,
    },
    organization_capabilities: {
      empty_directory_cleanup: { enabled: false, reason_code: "empty_directory_cleanup_disabled", reason_zh: reason, settings_section: "organization" },
    },
  });
  await page.goto("/library");
  await expect(page.getByRole("button", { name: "预览空目录清理" })).toBeDisabled();
  await expect(page.getByText(reason)).toBeVisible();
  await page.getByRole("button", { name: "前往自动整理设置" }).click();
  await expect(page).toHaveURL(/\/settings$/);
});
