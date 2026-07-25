import type { InspectionBatchResponse, InspectionBatchStatus, InspectionResult, InspectionResultStatus, ResourceSummary } from "./types";

export const INSPECTION_POLL_INTERVAL_MS = 1_500;
export const INSPECTION_TIMEOUT_MS = 10 * 60 * 1_000;
export const INSPECTION_BATCH_SIZE = 8;
export const MAX_INSPECTABLE_MAGNETS = 30;

export interface InspectionProgress {
  completed: number;
  submitted: number;
  failed: number;
}

export type InspectionPollState = InspectionBatchStatus | "timeout" | "stale";

export interface InspectionPollOptions {
  isCurrent: () => boolean;
  onResponse: (response: InspectionBatchResponse) => void;
  now?: () => number;
  sleep?: (milliseconds: number) => Promise<void>;
  intervalMs?: number;
  timeoutMs?: number;
}

export function inspectionProgress(response: InspectionBatchResponse): InspectionProgress {
  return {
    completed: response.completed_count,
    submitted: response.submitted_count,
    failed: response.results.filter((item) => item.status === "failed" || item.status === "timeout").length,
  };
}

export function inspectionState(response: InspectionBatchResponse): InspectionBatchStatus {
  return response.status;
}

export function inspectionResultEnded(result: InspectionResult): boolean {
  return result.status === "verified"
    || result.status === "unsupported"
    || result.status === "failed"
    || result.status === "timeout";
}

export function inspectionStatusLabel(status: InspectionResultStatus): string {
  const labels: Record<InspectionResultStatus, string> = {
    verified: "已检测",
    unsupported: "无法检测",
    timeout: "检测超时",
    failed: "检测失败",
  };
  return labels[status];
}

export function mergeInspectionResult(resource: ResourceSummary, result: InspectionResult): ResourceSummary {
  if (result.status !== "verified") {
    return { ...resource, inspection_status: result.status };
  }
  return {
    ...resource,
    size_bytes: result.total_size_bytes,
    size_source: "inspection",
    video_file_count: result.video_file_count,
    subtitle_count: result.subtitle_count,
    sample_count: result.sample_count,
    inspection_status: result.status,
  };
}

export function nextInspectionResourceIds(
  resources: ResourceSummary[],
  processedIds: ReadonlySet<string>,
  limit = INSPECTION_BATCH_SIZE,
): string[] {
  return resources
    .filter((resource) => resource.kind === "magnet")
    .slice(0, MAX_INSPECTABLE_MAGNETS)
    .filter((resource) => !processedIds.has(resource.resource_id))
    .slice(0, limit)
    .map((resource) => resource.resource_id);
}

export function finalizeInspectionResources(
  resources: ResourceSummary[],
  resourceIds: string[],
  status: "failed" | "timeout",
): ResourceSummary[] {
  const ids = new Set(resourceIds);
  return resources.map((resource) => ids.has(resource.resource_id)
    && (resource.inspection_status === "running" || resource.inspection_status === "queued")
    ? { ...resource, inspection_status: status }
    : resource);
}

export async function pollInspectionBatch(
  getInspection: (batchId: string, signal: AbortSignal) => Promise<InspectionBatchResponse>,
  batchId: string,
  options: InspectionPollOptions,
): Promise<InspectionPollState> {
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? ((milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds)));
  const intervalMs = options.intervalMs ?? INSPECTION_POLL_INTERVAL_MS;
  const deadline = now() + (options.timeoutMs ?? INSPECTION_TIMEOUT_MS);

  while (now() < deadline) {
    if (!options.isCurrent()) return "stale";
    const remaining = deadline - now();
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), remaining);
    let response: InspectionBatchResponse;
    try {
      response = await getInspection(batchId, controller.signal);
    } catch (error) {
      if (controller.signal.aborted) {
        return options.isCurrent() ? "timeout" : "stale";
      }
      throw error;
    } finally {
      clearTimeout(abortTimer);
    }
    if (!options.isCurrent()) return "stale";
    options.onResponse(response);
    if (!options.isCurrent()) return "stale";
    if (response.status === "completed" || response.status === "partial" || response.status === "failed") {
      return response.status;
    }
    const sleepRemaining = deadline - now();
    if (sleepRemaining <= 0) break;
    await sleep(Math.min(intervalMs, sleepRemaining));
  }
  return options.isCurrent() ? "timeout" : "stale";
}
