import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api";

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient season and inspection requests", () => {
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
});
