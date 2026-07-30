import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, focusFirstFieldError } from "../src/api";

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient season and inspection requests", () => {
  it("focuses the first structured field error by id or name", () => {
    document.body.innerHTML = '<input id="blocked_keywords" />';
    const error = new ApiError("输入内容有误", 422, "validation_error", {
      fieldErrors: [{ fieldId: "blocked_keywords", message: "字段内容格式不正确。" }],
    });

    focusFirstFieldError(error);

    expect(document.activeElement?.id).toBe("blocked_keywords");
  });

  it("preserves safe structured field errors from the backend", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: {
        code: "validation_error",
        field_errors: [{ field_id: "blocked_keywords", message_zh: "字段内容格式不正确。" }],
      },
    }), { status: 422 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await expect(api.mediaMetadata("movie", 1)).rejects.toMatchObject({
      code: "validation_error",
      fieldErrors: [{ fieldId: "blocked_keywords", message: "字段内容格式不正确。" }],
    });
  });

  it("uses notification list, read, and preference endpoints", async () => {
    const responses = [
      { items: [], unread_count: 0 },
      { id: "notification-1", event_code: "workflow.completed", severity: "info", title_zh: "完成", message_zh: "已完成", action_type: null, action_id: null, aggregate_count: 1, read_at: "2026-07-29T01:00:00Z", created_at: "2026-07-29T01:00:00Z", updated_at: "2026-07-29T01:00:00Z" },
      { marked_count: 1 },
      { enabled: true, muted_event_codes: [], revision: 2 },
      { enabled: false, muted_event_codes: ["storage.threshold"], revision: 3 },
    ];
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(responses.shift()), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.notifications(true, 10);
    await api.markNotificationRead("notification/1");
    await api.markAllNotificationsRead();
    await api.notificationPreferences();
    await api.updateNotificationPreferences({ enabled: false, muted_event_codes: ["storage.threshold"], revision: 2 });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/notifications?unread_only=true&limit=10",
      "/api/v1/notifications/notification%2F1/read",
      "/api/v1/notifications/read-all",
      "/api/v1/notification-preferences",
      "/api/v1/notification-preferences",
    ]);
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
    expect(fetchMock.mock.calls[2][1].method).toBe("POST");
    expect(fetchMock.mock.calls[4][1].method).toBe("PATCH");
    expect(JSON.parse(fetchMock.mock.calls[4][1].body as string)).toEqual({ enabled: false, muted_event_codes: ["storage.threshold"], revision: 2 });
  });

  it("starts and polls an independent resource search task", async () => {
    const task = {
      task_id: "resource_search_1",
      tmdb_id: 1399,
      media_type: "tv",
      season_number: 2,
      status: "running",
      snapshot_revision: null,
      query_plan_version: "v4",
      cache_age_seconds: null,
      sources: [],
      selected_season: 2,
      warnings: [],
      error_code: null,
      created_at: "2026-07-29T01:00:00Z",
      updated_at: "2026-07-29T01:00:00Z",
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(task), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...task, status: "ready", snapshot_revision: "snap-1", sources: ["plugin:magnet"] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.startResourceSearch("tv", 1399, { seasonNumber: 2 });
    await api.resourceSearch("resource_search_1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/media/tv/1399/resource-search",
      "/api/v1/resource-search/resource_search_1",
    ]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ season_number: 2, refresh: false });
  });

  it("associates inspection and push requests with a workflow", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "wf_1" }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "inspect_1" }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "task_1", workflow_id: "wf_1" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.createWorkflow({ mediaType: "movie", tmdbId: 27205 });
    await api.inspectResources(["resource_1"], "wf_1");
    await api.createTask("resource_1", false, "wf_1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/workflows",
      "/api/v1/resources/inspect",
      "/api/v1/tasks",
    ]);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toEqual({
      resource_ids: ["resource_1"],
      workflow_id: "wf_1",
    });
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string)).toEqual({
      resource_id: "resource_1",
      force: false,
      workflow_id: "wf_1",
    });
  });

  it("converts an unknown backend detail to a safe Chinese error", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "python traceback secret" }), { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await expect(api.mediaMetadata("movie", 1)).rejects.toMatchObject({
      code: "unknown_error",
      message: "本次操作未完成，当前页面没有更新。",
      retryable: true,
    });
  });

  it("loads media metadata independently from resource search", async () => {
    const response = { tmdb_id: 1399, media_type: "tv", title: "权力的游戏", original_title: "Game of Thrones", release_year: 2011, overview: "剧集简介", poster_path: null, backdrop_path: null, genre_ids: [], vote_average: 8.2, seasons: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();
    const controller = new AbortController();

    await api.mediaMetadata("tv", 1399, controller.signal);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/media/tv/1399");
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it("records bounded media detail performance metrics", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ accepted: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.recordMediaDetailMetric("movie", 1399, {
      stage: "metadata_complete",
      status: "success",
      duration_ms: 87,
      cached: true,
      season_number: 2,
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/media/movie/1399/performance");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({
      stage: "metadata_complete",
      status: "success",
      duration_ms: 87,
      cached: true,
      season_number: 2,
    });
  });

  it("loads workflow summaries and a workflow timeline through separate endpoints", async () => {
    const list = { items: [], page: 1, page_size: 20, total: 0 };
    const detail = { id: "wf_1", correlation_id: "corr_1", media_type: "movie", tmdb_id: 1, subscription_id: null, status: "completed", status_zh: "已完成", state_reason: null, created_at: "2026-07-29T00:00:00Z", updated_at: "2026-07-29T00:00:00Z", stages: [] };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(list), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(detail), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.workflows({ status: "in_progress" });
    await api.workflow("wf_1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/workflows?page=1&page_size=20&status=in_progress",
      "/api/v1/workflows/wf_1",
    ]);
  });

  it("connects the library scan and STRM workbench endpoints", async () => {
    const responses = [
      { items: [], next_cursor: null },
      { library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: false, scope_verified: false, revision: 1, latest_scan: null },
      { library: { library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: true, scope_verified: true, revision: 2, latest_scan: null }, verified: true, enabled: true },
      { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
      { plan_id: "plan-1", plan_hash: "a".repeat(64), status: "needs_review", revision: 1, expires_at: "2026-08-01T00:00:00Z", source_count: 1, action_count: 1, precondition_count: 1, alias: null },
      { items: [], next_cursor: null },
      { items: [], page: 1, page_size: 50, total: 0, total_pages: 0 },
      { library_id: "main", scan_run_id: "scan-1", generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0 },
      { library_id: "main", scan_run_id: "scan-1", generated: 0, unchanged: 1, skipped: 0, failed: 0, retired: 0 },
      { library_id: "main", scan_run_id: "scan-1", generated: 0, unchanged: 0, skipped: 0, failed: 0, retired: 1 },
    ];
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(responses.shift()), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.libraries();
    await api.configureLibrary("main", { name: "115 媒体库", root_directory_id: "123", revision: 0 });
    await api.verifyLibraryScope("main");
    await api.scanLibrary("main");
    await api.createOrganizationPreview("main", "scan-1");
    await api.libraryMedia("main");
    await api.strmManifest("main");
    await api.generateStrm("main", "scan-1");
    await api.incrementalStrm("main", "scan-1");
    await api.cleanupStrm("main", "scan-1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/libraries?cursor=0&limit=50",
      "/api/v1/libraries/main/configuration",
      "/api/v1/libraries/main/verify-scope",
      "/api/v1/libraries/main/scan",
      "/api/v1/libraries/main/organization-preview",
      "/api/v1/libraries/main/media?cursor=0&limit=50",
      "/api/v1/libraries/main/strm-manifest?page=1&page_size=50",
      "/api/v1/libraries/main/strm-generation",
      "/api/v1/libraries/main/strm-incremental",
      "/api/v1/libraries/main/strm-cleanup",
    ]);
    expect(JSON.parse(fetchMock.mock.calls[3][1].body as string).idempotency_key).toMatch(/^(wa-|[0-9a-f-]{36}$)/);
  });

  it("sends workflow filters and guarded approval/cancel actions", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "wf_1", status: "in_progress", stages: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "wf_1", status: "cancelled", stages: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.workflows({ subscriptionId: "sub_1", stage: "approval", stageStatus: "waiting_confirmation" });
    await api.decideWorkflowApproval("wf_1", "approve");
    await api.cancelWorkflow("wf_1");

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.body])).toEqual([
      ["/api/v1/workflows?page=1&page_size=20&subscription_id=sub_1&stage=approval&stage_status=waiting_confirmation", undefined],
      ["/api/v1/workflows/wf_1/approval", JSON.stringify({ decision: "approve" })],
      ["/api/v1/workflows/wf_1/cancel", JSON.stringify({})],
    ]);
  });

  it("loads independent season metadata with a cache-safe query", async () => {
    const response = { series_tmdb_id: 1399, tmdb_season_id: 456, season_number: 2, name: "第 2 季", overview: "本季简介", overview_language: "zh-CN", poster_path: null, air_date: "2025-01-01", episode_count: 10, vote_average: 8.1, source: "tmdb", fetched_at: "2026-07-29T00:00:00Z", cached: false, stale: false, data_version: 1, episodes: [], warnings: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();
    const controller = new AbortController();

    await api.seasonMetadata(1399, 2, { refresh: true }, controller.signal);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/media/tv/1399/seasons/2?language=zh-CN&fallback_language=en-US&refresh=true");
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it("sends the frozen resource pagination query and signal", async () => {
    const response = { items: [], page: 2, page_size: 50, total: 501, total_pages: 11, facets: { magnet: 500, share: 1, "4k": 100, "1080p": 300, "720p": 50, subtitle: 80 }, snapshot_revision: "snapshot-1" };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();
    const controller = new AbortController();

    await api.resources("tv", 1399, { seasonNumber: 0, kind: "magnet", quality: "1080p", query: "  show  ", sort: "relevance", page: 2, pageSize: 50 }, controller.signal);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/media/tv/1399/resources?sort=relevance&page=2&page_size=50&season_number=0&kind=magnet&quality=1080p&query=show");
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it("sends a TV season and accepts old health responses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ movie: {}, results: [], warnings: [], cached: false, cache_age_seconds: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.search(1399, "tv", false, 2);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toMatchObject({ tmdb_id: 1399, media_type: "tv", season_number: 2 });

    const health = await api.health();
    expect(health.inspection_supported).toBeUndefined();
  });

  it("uses the inspection batch POST and GET endpoints", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "batch-1", status: "queued", submitted_count: 2, completed_count: 0, results: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "batch-1", status: "completed", submitted_count: 2, completed_count: 2, results: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.inspectResources(["magnet-1", "magnet-2"]);
    await api.getInspection("batch-1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/resources/inspect");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ resource_ids: ["magnet-1", "magnet-2"] });
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/resources/inspect/batch-1");
  });

  it("passes an AbortSignal to inspection GET requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ batch_id: "batch-1", status: "running", submitted_count: 1, completed_count: 0, results: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();
    const controller = new AbortController();

    await api.getInspection("batch-1", controller.signal);

    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it("uses the completed settings contract", async () => {
    const responses = [
      { release: "2026.07.25", uptime_seconds: 1, database_size_bytes: 456, capabilities: { inspection: true, magnet: true, share: false } },
      { revision: 0, level: "INFO", retention_days: 30, max_file_mb: 10 },
      { revision: 1, level: "WARNING", retention_days: 45, max_file_mb: 20 },
      { auto_start_enabled: true, revision: 0 },
      { auto_start_enabled: false, revision: 1 },
      { enabled: true, ready: true, capabilities: { magnet: true, share: false }, cookie: { source: "tgtodrive", configured: true, structure_valid: true, sync_status: "success", last_sync_at: null }, target_configured: true, max_concurrency: 1 },
      { status: "needs_auth", checked_at: "2026-07-25T02:00:00Z" },
      { items: [], next_cursor: 20 },
    ];
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(responses.shift()), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.settingsOverview();
    await api.loggingSettings();
    await api.updateLoggingSettings({ revision: 0, level: "WARNING", retention_days: 45, max_file_mb: 20 });
    await api.inspectionSettings();
    await api.updateInspectionSettings({ revision: 0, auto_start_enabled: false });
    await api.p115Settings();
    await api.validateP115Cookie();
    await api.logs({ category: "system", cursor: 20, limit: 20 });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/settings/overview",
      "/api/v1/settings/logging",
      "/api/v1/settings/logging",
      "/api/v1/settings/inspection",
      "/api/v1/settings/inspection",
      "/api/v1/settings/p115",
      "/api/v1/settings/p115/validate",
      "/api/v1/logs?limit=20&cursor=20&category=system",
    ]);
    expect(fetchMock.mock.calls[1][1].method).toBeUndefined();
    expect(fetchMock.mock.calls[2][1].method).toBe("PATCH");
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string)).toEqual({ revision: 0, level: "WARNING", retention_days: 45, max_file_mb: 20 });
    expect(fetchMock.mock.calls[3][1].method).toBeUndefined();
    expect(fetchMock.mock.calls[4][1].method).toBe("PATCH");
    expect(fetchMock.mock.calls[5][1].method).toBeUndefined();
    expect(fetchMock.mock.calls[6][1].method).toBe("POST");
  });

  it("uses the managed credentials endpoints without expecting secret response fields", async () => {
    const response = { revision: 3, tmdb: { configured: true, source: "managed", last_updated_at: "2026-07-25T03:00:00Z" }, p115_cookie: { configured: true, source: "managed", last_updated_at: "2026-07-25T03:00:00Z", structure_valid: true, ready: true } };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient();

    await api.credentialSettings();
    await api.updateTmdbCredential("test-tmdb-key", 3);
    await api.resetTmdbCredential(4);
    await api.updateP115Cookie("UID=test-cookie", 5);
    await api.resetP115Cookie(6);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/settings/credentials",
      "/api/v1/settings/credentials/tmdb",
      "/api/v1/settings/credentials/tmdb/reset",
      "/api/v1/settings/credentials/p115-cookie",
      "/api/v1/settings/credentials/p115-cookie/reset",
    ]);
    expect(fetchMock.mock.calls[1][1].method).toBe("PUT");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toEqual({ value: "test-tmdb-key", revision: 3 });
    expect(fetchMock.mock.calls[2][1].method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string)).toEqual({ revision: 4 });
    expect(fetchMock.mock.calls[3][1].method).toBe("PUT");
    expect(JSON.parse(fetchMock.mock.calls[3][1].body as string)).toEqual({ value: "UID=test-cookie", revision: 5 });
    expect(fetchMock.mock.calls[4][1].method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[4][1].body as string)).toEqual({ revision: 6 });
  });
});
