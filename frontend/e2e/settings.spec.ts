import { expect, test } from "@playwright/test";

const overview = {
  release: "2026.07.25",
  uptime_seconds: 90061,
  database_size_bytes: 3145728,
  capabilities: { inspection: true, magnet: true, share: false },
};
const p115 = {
  enabled: true,
  ready: true,
  capabilities: { magnet: true, share: false },
  cookie: {
    source: "tgtodrive",
    configured: true,
    structure_valid: true,
    sync_status: "success",
    last_sync_at: "2026-07-25T02:00:00Z",
  },
  target_configured: true,
  max_concurrency: 1,
};
const credentialsSnapshot = (revision: number, tmdbSource: "environment" | "managed" = "environment", p115Source: "tgtodrive" | "managed" = "tgtodrive") => ({
  revision,
  tmdb: { configured: tmdbSource === "managed", source: tmdbSource, last_updated_at: tmdbSource === "managed" ? "2026-07-25T03:00:00Z" : null },
  p115_cookie: { configured: true, source: p115Source, last_updated_at: "2026-07-25T03:00:00Z", structure_valid: true, ready: true },
});
const prowlarrSnapshot = (revision: number, source: "none" | "managed" | "environment" = "none", configured = false) => ({
  source,
  enabled: source !== "none",
  configured,
  base_url: source === "none" ? null : "http://prowlarr:9696",
  api_key_configured: configured,
  api_key_source: configured ? source : "none",
  last_updated_at: revision ? "2026-08-01T10:00:00Z" : null,
  revision,
});

test("settings contract, cursor logs, validation states, and responsive layout", async ({ page }, testInfo) => {
  let loggingRevision = 0;
  let saveCount = 0;
  let validateCount = 0;
  let logsCount = 0;
  let conflictNextSave = true;
  let credentialRevision = 0;
  let credentialConflictNextSave = false;
  let credentialSaveCount = 0;
  let holdCredentialSave = false;
  let releaseCredentialSave!: () => void;
  let prowlarrRevision = 0;
  let prowlarrSource: "none" | "managed" | "environment" = "none";
  let prowlarrConfigured = false;
  let prowlarrConflictNextSave = false;
  const prowlarrBodies: unknown[] = [];
  const credentialBodies: unknown[] = [];
  const credentialGate = new Promise<void>((resolve) => { releaseCredentialSave = resolve; });
  const patchBodies: unknown[] = [];
  const validationStatuses = ["ready", "needs_auth", "unavailable"] as const;

  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-settings" } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] } }));
  await page.route("**/api/v1/settings/overview", (route) => route.fulfill({ json: overview }));
  await page.route("**/api/v1/settings/search-sources/prowlarr", async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({ json: prowlarrSnapshot(prowlarrRevision, prowlarrSource, prowlarrConfigured) });
      return;
    }
    expect(request.method()).toBe("PATCH");
    const body = request.postDataJSON();
    prowlarrBodies.push(body);
    if (prowlarrConflictNextSave) {
      prowlarrConflictNextSave = false;
      await route.fulfill({ status: 409, json: { detail: "settings_conflict" } });
      return;
    }
    expect(body.revision).toBe(prowlarrRevision);
    prowlarrRevision += 1;
    prowlarrSource = "managed";
    prowlarrConfigured = true;
    await route.fulfill({ json: prowlarrSnapshot(prowlarrRevision, "managed", true) });
  });
  await page.route("**/api/v1/settings/search-sources/prowlarr/reset", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ revision: prowlarrRevision });
    prowlarrRevision += 1;
    prowlarrSource = "none";
    prowlarrConfigured = false;
    await route.fulfill({ json: prowlarrSnapshot(prowlarrRevision, "none", false) });
  });
  await page.route("**/api/v1/settings/search-sources/prowlarr/verify", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({});
    await route.fulfill({ json: { source: "prowlarr", status: "available", configured: true, base_url: "http://prowlarr:9696", message_code: null, checked_at: "2026-08-01T10:01:00Z" } });
  });
  await page.route("**/api/v1/settings/credentials**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/settings/credentials")) {
      await route.fulfill({ json: credentialsSnapshot(credentialRevision) });
      return;
    }
    expect(request.method()).toMatch(/PUT|POST/);
    credentialSaveCount += 1;
    credentialBodies.push(request.postDataJSON());
    if (holdCredentialSave) await credentialGate;
    if (credentialConflictNextSave) {
      credentialConflictNextSave = false;
      return route.fulfill({ status: 409, json: { detail: "settings_conflict" } });
    }
    credentialRevision += 1;
    const isTmdb = path.endsWith("/tmdb");
    const isTmdbReset = path.endsWith("/tmdb/reset");
    const isP115Reset = path.endsWith("/p115-cookie/reset");
    return route.fulfill({ json: credentialsSnapshot(credentialRevision, isTmdb || isTmdbReset ? (isTmdbReset ? "environment" : "managed") : credentialRevision > 0 ? "managed" : "environment", isP115Reset ? "tgtodrive" : credentialRevision > 0 ? "managed" : "tgtodrive") });
  });
  await page.route("**/api/v1/settings/logging", (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: { revision: loggingRevision, level: "INFO", retention_days: 30, max_file_mb: 10 } });
    expect(route.request().method()).toBe("PATCH");
    patchBodies.push(route.request().postDataJSON());
    saveCount += 1;
    if (conflictNextSave) {
      conflictNextSave = false;
      return route.fulfill({ status: 409, json: { detail: "revision conflict" } });
    }
    loggingRevision = 1;
    return route.fulfill({ json: { revision: loggingRevision, level: "WARNING", retention_days: 45, max_file_mb: 20 } });
  });
  await page.route("**/api/v1/settings/p115/validate", (route) => {
    const status = validationStatuses[validateCount] ?? "unavailable";
    validateCount += 1;
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({});
    return route.fulfill({ json: { status, checked_at: "2026-07-25T02:00:00Z" } });
  });
  await page.route("**/api/v1/settings/p115", (route) => route.fulfill({ json: p115 }));
  await page.route("**/api/v1/settings/p115/devices", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/settings/organization", (route) => route.fulfill({ json: {
    revision: 0,
    schedule_enabled: false,
    scan_interval_minutes: 60,
    source_directory_ids: [],
    target_directory_id: null,
    push_directory_id: null,
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
  await page.route("**/api/v1/logs?**", (route) => {
    logsCount += 1;
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("limit")).toBe("20");
    expect(params.get("level")).toBeNull();
    if (params.get("category") === "search") return route.fulfill({ json: { items: [{ id: 9, timestamp: "2026-07-25T02:03:00Z", level: "INFO", category: "search", message: "新分类响应" }], next_cursor: null } });
    if (params.get("cursor") === "20") return route.fulfill({ json: { items: [{ id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "缓存已刷新（重复）" }, { id: 3, timestamp: "2026-07-25T02:02:00Z", level: "ERROR", category: "security", message: "需要重新登录" }], next_cursor: null } });
    expect(params.get("cursor")).toBeNull();
    return route.fulfill({ json: { items: [{ id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO", category: "system", message: "服务已启动" }, { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING", category: "cache", message: "缓存已刷新" }], next_cursor: 20 } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await expect(page.getByText("2026.07.25")).toBeVisible();
  await expect(page.getByText("内容检测")).toBeVisible();

  const mobileSectionSelect = page.locator(".settings-mobile-select select");
  const mobileLayout = await mobileSectionSelect.isVisible();
  if (mobileLayout) await mobileSectionSelect.selectOption("prowlarr");
  else await page.getByRole("button", { name: "搜索来源" }).click();
  await expect(page.getByRole("heading", { name: "Prowlarr" })).toBeVisible();
  await expect(page.locator(".prowlarr-status-line > span:nth-of-type(2)")).toHaveText("未启用");
  const submittedProwlarrValue = "fixture-only";
  await page.getByLabel("服务地址").fill("http://prowlarr:9696");
  await page.getByLabel("API Key").fill(submittedProwlarrValue);
  await page.getByLabel("启用 Prowlarr 搜索来源").check();
  await page.getByRole("button", { name: "保存 Prowlarr 配置" }).click();
  await expect(page.getByText("Prowlarr 配置已保存")).toBeVisible();
  expect(prowlarrBodies[0]).toEqual({ enabled: true, base_url: "http://prowlarr:9696", api_key: submittedProwlarrValue, revision: 0 });
  await expect(page.getByLabel("API Key")).toHaveValue("");
  expect(await page.locator("html").textContent()).not.toContain(submittedProwlarrValue);
  await page.getByRole("button", { name: "验证 Prowlarr 连接" }).click();
  await expect(page.getByText("连接验证成功")).toBeVisible();
  await page.getByRole("button", { name: "恢复环境配置" }).click();
  await expect(page.getByText("已恢复环境配置")).toBeVisible();
  prowlarrConflictNextSave = true;
  await page.getByLabel("服务地址").fill("http://changed:9696");
  await page.getByRole("button", { name: "保存 Prowlarr 配置" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toBeVisible();
  await page.getByRole("button", { name: "重新加载" }).last().click();
  await expect(page.getByLabel("服务地址")).toHaveValue("");

  if (mobileLayout) await mobileSectionSelect.selectOption("logs");
  else await page.getByRole("button", { name: "日志" }).click();
  const logMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "服务已启动" }) : page.locator(".settings-log-table td").filter({ hasText: "服务已启动" });
  await expect(logMessage).toBeVisible();
  await expect.poll(() => logsCount).toBe(1);
  await page.getByRole("button", { name: "加载更多" }).click();
  const laterMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "需要重新登录" }) : page.locator(".settings-log-table td").filter({ hasText: "需要重新登录" });
  await expect(laterMessage).toBeVisible();
  await expect(page.getByText("已加载 3 条")).toBeVisible();
  await page.locator(".settings-filter-row select").first().selectOption("search");
  const categoryMessage = mobileLayout ? page.locator(".settings-log-item p").filter({ hasText: "新分类响应" }) : page.locator(".settings-log-table td").filter({ hasText: "新分类响应" });
  await expect(categoryMessage).toBeVisible();
  await expect(page.getByText("服务已启动")).toHaveCount(0);
  await expect.poll(() => logsCount).toBe(3);

  await page.getByLabel("最低级别").selectOption("WARNING");
  await page.getByLabel("保留天数（1-90）").fill("45");
  await page.getByLabel("文件上限（MB，1-50）").fill("20");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toBeVisible();
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toHaveCount(0);
  await page.getByLabel("最低级别").selectOption("WARNING");
  await page.getByLabel("保留天数（1-90）").fill("45");
  await page.getByLabel("文件上限（MB，1-50）").fill("20");
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => saveCount).toBe(2);
  expect(patchBodies[0]).toEqual({ revision: 2, level: "WARNING", retention_days: 45, max_file_mb: 20 });

  if (mobileLayout) await mobileSectionSelect.selectOption("p115");
  else await page.getByRole("button", { name: "115 推送" }).click();
  await expect(page.getByText("已就绪")).toBeVisible();
  await expect(page.getByText("TgtoDrive")).toBeVisible();
  await expect(page.getByText("结构正常")).toBeVisible();
  await expect(page.getByText("115 分享转存")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("Cookie 已就绪")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("Cookie 需要重新授权")).toBeVisible();
  await page.getByRole("button", { name: "验证 Cookie" }).click();
  await expect(page.getByText("115 当前不可用")).toBeVisible();
  await expect(page.getByText("结构正常")).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  if (mobileLayout) await mobileSectionSelect.selectOption("credentials");
  else await page.getByRole("button", { name: "连接配置" }).click();
  await expect(page.getByRole("heading", { name: "连接配置" })).toBeVisible();
  const credentialPanels = page.locator(".credential-panel");
  const tmdbSecret = "test-tmdb-key-123";
  const cookieSecret = "UID=test-cookie; CID=fake";
  await credentialPanels.nth(0).getByLabel("TMDB API Key").fill(tmdbSecret);
  await credentialPanels.nth(0).getByRole("button", { name: "保存并验证" }).click();
  await expect(credentialPanels.nth(0).getByLabel("TMDB API Key")).toHaveValue("");
  await credentialPanels.nth(1).getByLabel("P115 Cookie").fill(cookieSecret);
  await credentialPanels.nth(1).getByRole("button", { name: "保存并验证" }).click();
  await expect(credentialPanels.nth(1).getByLabel("P115 Cookie")).toHaveValue("");
  expect(credentialBodies).toContainEqual({ value: tmdbSecret, revision: 0 });
  expect(credentialBodies).toContainEqual({ value: cookieSecret, revision: 1 });

  await credentialPanels.nth(0).getByRole("button", { name: "恢复环境配置" }).click();
  await credentialPanels.nth(1).getByRole("button", { name: "恢复 TgtoDrive" }).click();
  await expect.poll(() => credentialSaveCount).toBe(4);
  credentialConflictNextSave = true;
  await credentialPanels.nth(0).getByLabel("TMDB API Key").fill("draft-conflict-only");
  await credentialPanels.nth(0).getByRole("button", { name: "保存并验证" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toBeVisible();
  await expect(credentialPanels.nth(0).getByLabel("TMDB API Key")).toHaveValue("draft-conflict-only");
  await credentialPanels.nth(0).getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("设置已被其他请求修改")).toHaveCount(0);

  holdCredentialSave = true;
  await credentialPanels.nth(0).getByLabel("TMDB API Key").fill("test-duplicate-secret");
  const duplicateButton = credentialPanels.nth(0).getByRole("button", { name: "保存并验证" });
  const requestsBeforeDuplicate = credentialSaveCount;
  await duplicateButton.click();
  await expect(duplicateButton).toBeDisabled();
  await duplicateButton.dispatchEvent("click");
  await expect.poll(() => credentialSaveCount).toBe(requestsBeforeDuplicate + 1);
  releaseCredentialSave();
  holdCredentialSave = false;
  await expect(credentialPanels.nth(0).getByLabel("TMDB API Key")).toHaveValue("");
  await credentialPanels.nth(1).getByRole("button", { name: "验证当前 Cookie" }).click();
  await expect(page.getByText("115 当前不可用")).toBeVisible();
  const html = await page.locator("html").evaluate((element) => element.outerHTML);
  expect(html).not.toContain(tmdbSecret);
  expect(html).not.toContain(cookieSecret);
  expect(html).not.toContain("test-duplicate-secret");

  await page.evaluate(() => window.scrollTo(0, 0));
  const layout = await page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth, contentRight: document.querySelector(".settings-content")?.getBoundingClientRect().right ?? 0 }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.contentRight).toBeLessThanOrEqual(layout.clientWidth + 1);
  await page.screenshot({ path: testInfo.outputPath(`settings-${testInfo.project.name}.png`), fullPage: true });
});

test("keeps release, directory IDs, and machine codes out of settings main prompts", async ({ page }) => {
  const fullRelease = "0123456789abcdef0123456789abcdef01234567";
  const fullDirectoryId = "9876543210987654321";
  const errorCode = "scan_incomplete";
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "fixture-csrf" } }));
  await page.route("**/api/v1/movies/home", (route) => route.fulfill({ json: { popular: [], now_playing: [], upcoming: [], top_rated: [], tv_popular: [], tv_on_the_air: [], tv_top_rated: [] } }));
  await page.route("**/api/v1/settings/overview", (route) => route.fulfill({ json: {
    release: fullRelease,
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
  await page.route("**/api/v1/settings/logging", (route) => route.fulfill({ json: { revision: 0, level: "INFO", retention_days: 30, max_file_mb: 10 } }));
  await page.route("**/api/v1/settings/inspection", (route) => route.fulfill({ json: { auto_start_enabled: false, revision: 0 } }));
  await page.route("**/api/v1/settings/p115", (route) => route.fulfill({ json: p115 }));
  await page.route("**/api/v1/settings/p115/devices", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/settings/search-sources/prowlarr", (route) => route.fulfill({ json: prowlarrSnapshot(0) }));
  await page.route("**/api/v1/settings/credentials", (route) => route.fulfill({ json: credentialsSnapshot(0) }));
  await page.route("**/api/v1/settings/content-policy", (route) => route.fulfill({ json: { hide_adult_media: true, hide_suspicious_resources: true, hide_low_quality_resources: true, blocked_keywords: [], revision: 0 } }));
  await page.route("**/api/v1/settings/organization", (route) => route.fulfill({ json: {
    revision: 0,
    schedule_enabled: false,
    scan_interval_minutes: 60,
    source_directory_ids: [fullDirectoryId],
    source_directory_labels: ["待整理/来源"],
    target_directory_id: fullDirectoryId,
    target_directory_label: "媒体库/归档",
    push_directory_id: fullDirectoryId,
    push_directory_label: "媒体库/推送",
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
    status: "failed",
    available_statuses: ["unknown", "success", "skipped", "deleted", "replace", "failed"],
    source_count: 1,
    scanned_count: 0,
    plan_count: 0,
    queued_count: 0,
    blocked_count: 1,
    blocked_details: [{ source_directory_id: fullDirectoryId, phase: "scan", error_code: errorCode, message_zh: "扫描未完成。", next_step_zh: "请重新扫描。" }],
    items: [],
    finished_at: null,
    run_id: null,
  } }));
  await page.route("**/api/v1/logs?**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await expect(page.getByText(fullRelease)).toHaveCount(0);
  await expect(page.getByText(fullRelease.slice(0, 7))).toBeVisible();

  const organizationButton = page.getByRole("button", { name: "115 整理" });
  if (await organizationButton.isVisible()) {
    await organizationButton.click();
  } else {
    await page.locator(".settings-mobile-select select").selectOption("organization");
  }
  await expect(page.getByRole("heading", { name: "自动整理" })).toBeVisible();
  await expect(page.getByText(fullDirectoryId)).toHaveCount(0);
  await expect(page.getByText("待整理/来源")).toBeVisible();
  await expect(page.getByText("扫描未完成。", { exact: false })).toBeVisible();
  await expect(page.getByText(errorCode, { exact: true })).toBeHidden();
  const diagnostics = page.locator(".organization-blocked-details details");
  await expect(diagnostics).not.toHaveAttribute("open", "");
  await expect(diagnostics).toContainText(errorCode);
});
