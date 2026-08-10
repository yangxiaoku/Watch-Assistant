import type { ResourceSearchResponse } from "./types";

export interface ResourceSearchPollClient {
  resourceSearch(taskId: string, signal?: AbortSignal): Promise<ResourceSearchResponse>;
}

export interface ResourceSearchPollOptions {
  requestId: number;
  currentRequestId: () => number;
  signal: AbortSignal;
  intervalMs?: number;
  maxAttempts?: number;
}

function waitForNextPoll(signal: AbortSignal, intervalMs: number): Promise<void> {
  return new Promise((resolve) => {
    let timer: number;
    const onAbort = () => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      resolve();
    };
    timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, intervalMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export async function waitForResourceSearch(
  client: ResourceSearchPollClient,
  task: ResourceSearchResponse,
  options: ResourceSearchPollOptions,
): Promise<ResourceSearchResponse | null> {
  const intervalMs = options.intervalMs ?? 250;
  // 服务端多来源搜索可达分钟级:上限放宽到 120s,超时返回 timeout 状态
  // 而非伪造 failed——failed 会让前端清空已加载快照。
  const maxAttempts = options.maxAttempts ?? 480;
  let current = task;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (options.currentRequestId() !== options.requestId || options.signal.aborted) return null;
    if (current.status === "ready" || current.status === "failed") return current;
    await waitForNextPoll(options.signal, intervalMs);
    if (options.currentRequestId() !== options.requestId || options.signal.aborted) return null;
    current = await client.resourceSearch(current.task_id, options.signal);
  }
  return {
    ...current,
    status: "timeout",
    error_code: "resource_search_timeout",
  };
}
