import { afterEach, describe, expect, it, vi } from "vitest";

import {
  inspectionProgress,
  inspectionResultEnded,
  inspectionState,
  inspectionStatusLabel,
  mergeInspectionResult,
  pollInspectionBatch,
} from "../src/inspection";
import type { InspectionBatchResponse, InspectionResult, ResourceSummary } from "../src/types";

const emptyResult = (resourceId: string, status: InspectionResult["status"]): InspectionResult => ({
  resource_id: resourceId,
  infohash: `hash-${resourceId}`,
  status,
  total_size_bytes: 0,
  file_count: 0,
  video_file_count: 0,
  video_size_bytes: 0,
  subtitle_count: 0,
  sample_count: 0,
  largest_video_name: null,
  content_summary: null,
  error_code: status === "verified" ? null : status.toUpperCase(),
});

const batch = (overrides: Partial<InspectionBatchResponse> = {}): InspectionBatchResponse => ({
  batch_id: "batch-1",
  status: "running",
  submitted_count: 30,
  completed_count: 2,
  results: [],
  ...overrides,
});

const resource: ResourceSummary = {
  resource_id: "resource-1",
  kind: "magnet",
  name: "Show S01",
  size_bytes: null,
  seeders: null,
  source: "test",
  captured_at: "2026-07-24T10:00:00Z",
};

describe("inspection contract", () => {
  afterEach(() => vi.useRealTimers());

  it("uses submitted_count/completed_count and counts failed results", () => {
    const response = batch({
      results: [emptyResult("resource-1", "failed"), emptyResult("resource-2", "timeout")],
    });

    expect(inspectionProgress(response)).toEqual({ completed: 2, submitted: 30, failed: 2 });
  });

  it("only merges verified size and preserves an unreturned resource", () => {
    const verified = mergeInspectionResult(resource, { ...emptyResult("resource-1", "verified"), total_size_bytes: 1073741824, video_file_count: 1, subtitle_count: 2, sample_count: 1 });
    const unsupported = mergeInspectionResult({ ...resource, resource_id: "resource-2", size_bytes: null }, emptyResult("resource-2", "unsupported"));

    expect(verified).toMatchObject({ size_bytes: 1073741824, video_file_count: 1, subtitle_count: 2, sample_count: 1, inspection_status: "verified" });
    expect(unsupported.size_bytes).toBeNull();
    expect(resource.inspection_status).toBeUndefined();
  });

  it("maps result statuses and keeps failed batches failed", () => {
    expect(inspectionStatusLabel("verified")).toBe("已检测");
    expect(inspectionStatusLabel("unsupported")).toBe("无法检测");
    expect(inspectionStatusLabel("timeout")).toBe("检测超时");
    expect(inspectionStatusLabel("failed")).toBe("检测失败");
    expect(inspectionState(batch({ status: "failed" }))).toBe("failed");
    expect(inspectionResultEnded(emptyResult("resource-1", "verified"))).toBe(true);
    expect(inspectionResultEnded(emptyResult("resource-2", "unsupported"))).toBe(true);
    expect(inspectionResultEnded(emptyResult("resource-3", "failed"))).toBe(true);
    expect(inspectionResultEnded(emptyResult("resource-4", "timeout"))).toBe(true);
  });

  it("stops polling the old batch after a season change", async () => {
    const getInspection = vi.fn().mockResolvedValue(batch());
    let selectedSeason = 2;
    const result = await pollInspectionBatch(getInspection, "batch-1", {
      isCurrent: () => selectedSeason === 2,
      onResponse: () => { selectedSeason = 1; },
      sleep: async () => undefined,
    });

    expect(result).toBe("stale");
    expect(getInspection).toHaveBeenCalledTimes(1);
  });

  it("does not timeout after 72 seconds", async () => {
    vi.useFakeTimers();
    const getInspection = vi.fn().mockResolvedValue(batch());
    let current = true;
    let settled = false;
    const pending = pollInspectionBatch(getInspection, "batch-1", {
      isCurrent: () => current,
      onResponse: () => undefined,
    }).then((state) => {
      settled = true;
      return state;
    });

    await vi.advanceTimersByTimeAsync(72_000);
    expect(settled).toBe(false);
    expect(getInspection.mock.calls.length).toBeGreaterThan(40);
    current = false;
    await vi.advanceTimersByTimeAsync(1_500);
    await expect(pending).resolves.toBe("stale");
  });
});
