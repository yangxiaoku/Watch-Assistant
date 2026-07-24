import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api";

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient season and inspection requests", () => {
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
});
