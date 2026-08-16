import { describe, expect, it, vi } from "vitest";
import { waitForResourceSearch, waitForSnapshotUpdate } from "../src/resourceSearchPolling";
import type { ResourceSearchResponse } from "../src/types";

function task(status: ResourceSearchResponse["status"] = "running"): ResourceSearchResponse {
  return {
    task_id: "resource_search_test",
    tmdb_id: 1,
    media_type: "movie",
    season_number: null,
    status,
    snapshot_revision: null,
    query_plan_version: "v5",
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

    // timeout 不是 failed:failed 会让前端清空已加载快照
    expect(response).toMatchObject({ status: "timeout", error_code: "resource_search_timeout" });
    expect(resourceSearch).toHaveBeenCalledTimes(2);
  });
});

describe("snapshot update polling", () => {
  it("returns the updated task when the slow merge bumps the snapshot revision", async () => {
    const ready = { ...task("ready"), snapshot_revision: "rev-1" };
    const updated = { ...ready, snapshot_revision: "rev-2" };
    const resourceSearch = vi.fn()
      .mockResolvedValueOnce(ready)
      .mockResolvedValueOnce(updated);
    const response = await waitForSnapshotUpdate({ resourceSearch }, ready, {
      requestId: 1,
      currentRequestId: () => 1,
      signal: new AbortController().signal,
      intervalMs: 0,
      maxAttempts: 5,
    });
    expect(response).toMatchObject({ status: "ready", snapshot_revision: "rev-2" });
    expect(resourceSearch).toHaveBeenCalledTimes(2);
  });

  it("returns null when the revision never changes within the attempt budget", async () => {
    const ready = { ...task("ready"), snapshot_revision: "rev-1" };
    const resourceSearch = vi.fn().mockResolvedValue(ready);
    const response = await waitForSnapshotUpdate({ resourceSearch }, ready, {
      requestId: 1,
      currentRequestId: () => 1,
      signal: new AbortController().signal,
      intervalMs: 0,
      maxAttempts: 3,
    });
    expect(response).toBeNull();
    expect(resourceSearch).toHaveBeenCalledTimes(3);
  });

  it("stops observing when the task leaves the ready state", async () => {
    const ready = { ...task("ready"), snapshot_revision: "rev-1" };
    const resourceSearch = vi.fn().mockResolvedValue(task("running"));
    const response = await waitForSnapshotUpdate({ resourceSearch }, ready, {
      requestId: 1,
      currentRequestId: () => 1,
      signal: new AbortController().signal,
      intervalMs: 0,
      maxAttempts: 5,
    });
    expect(response).toBeNull();
    expect(resourceSearch).toHaveBeenCalledTimes(1);
  });

  it("stops immediately when the browser wait is cancelled", async () => {
    const controller = new AbortController();
    const ready = { ...task("ready"), snapshot_revision: "rev-1" };
    const resourceSearch = vi.fn().mockResolvedValue(ready);
    controller.abort();
    await expect(waitForSnapshotUpdate({ resourceSearch }, ready, {
      requestId: 1,
      currentRequestId: () => 1,
      signal: controller.signal,
    })).resolves.toBeNull();
    expect(resourceSearch).not.toHaveBeenCalled();
  });
});
