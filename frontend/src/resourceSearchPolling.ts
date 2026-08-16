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

export interface SnapshotUpdatePollOptions {
  requestId: number;
  currentRequestId: () => number;
  signal: AbortSignal;
  intervalMs?: number;
  maxAttempts?: number;
}

/**
 * 观察搜索任务的快照修订是否变化(慢速索引器结果后台合并后 revision 更新)。
 *
 * 主搜索 ready 后调用:慢速补搜完成会写入新的快照 revision,此时返回最新任务
 * 让调用方重新加载资源列表;若无新结果则在有限轮次后返回 null 静默退出。
 * 任务状态离开 ready(失效重搜/失败)也立即停止观察。
 */
export async function waitForSnapshotUpdate(
  client: ResourceSearchPollClient,
  task: ResourceSearchResponse,
  options: SnapshotUpdatePollOptions,
): Promise<ResourceSearchResponse | null> {
  const intervalMs = options.intervalMs ?? 2500;
  const maxAttempts = options.maxAttempts ?? 24; // 默认最多观察约 60s
  let current = task;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (options.currentRequestId() !== options.requestId || options.signal.aborted) return null;
    await waitForNextPoll(options.signal, intervalMs);
    if (options.currentRequestId() !== options.requestId || options.signal.aborted) return null;
    current = await client.resourceSearch(current.task_id, options.signal);
    if (current.status !== "ready") return null;
    if (current.snapshot_revision !== task.snapshot_revision) return current;
  }
  return null;
}
