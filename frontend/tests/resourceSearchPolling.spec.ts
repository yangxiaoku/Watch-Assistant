import { describe, expect, it, vi } from "vitest";
import { waitForResourceSearch } from "../src/resourceSearchPolling";
import type { ResourceSearchResponse } from "../src/types";

function task(status: ResourceSearchResponse["status"] = "running"): ResourceSearchResponse {
  return {
    task_id: "resource_search_test",
    tmdb_id: 1,
    media_type: "movie",
    season_number: null,
    status,
    snapshot_revision: null,
    query_plan_version: "v4",
    cache_age_seconds: null,
    sources: [],
    selected_season: null,
    warnings: [],
    error_code: null,
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:00Z",
  };
}

describe("resource search polling", () => {
  it("stops immediately when the browser wait is cancelled", async () => {
    const controller = new AbortController();
    const resourceSearch = vi.fn().mockResolvedValue(task());
    controller.abort();

    await expect(waitForResourceSearch({ resourceSearch }, task(), {
      requestId: 1,
      currentRequestId: () => 1,
      signal: controller.signal,
    })).resolves.toBeNull();
    expect(resourceSearch).not.toHaveBeenCalled();
  });

  it("returns a stable timeout while leaving the service task untouched", async () => {
    const resourceSearch = vi.fn().mockResolvedValue(task());
    const response = await waitForResourceSearch({ resourceSearch }, task(), {
      requestId: 1,
      currentRequestId: () => 1,
      signal: new AbortController().signal,
      intervalMs: 0,
      maxAttempts: 2,
    });

    expect(response).toMatchObject({ status: "failed", error_code: "resource_search_timeout" });
    expect(resourceSearch).toHaveBeenCalledTimes(2);
  });
});
