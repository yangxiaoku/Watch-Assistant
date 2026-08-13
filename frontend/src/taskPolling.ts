import type { TaskResponse } from "./types";

// 提交成功后的 submitted/downloading 由后端 watcher 周期推进(分钟级),
// 不再用 2 秒前端轮询假装"正在处理"。只有排队/提交中才需要高频刷新。
export const ACTIVE_TASK_STATES = ["queued", "submitting"] as const;

export function isActiveTask(task: Pick<TaskResponse, "state">): boolean {
  return ACTIVE_TASK_STATES.includes(task.state as (typeof ACTIVE_TASK_STATES)[number]);
}

export function createTaskRefreshGuard() {
  const versions = new Map<string, number>();

  return {
    begin(taskId: string): number {
      const version = (versions.get(taskId) ?? 0) + 1;
      versions.set(taskId, version);
      return version;
    },
    invalidate(taskId: string): number {
      const version = (versions.get(taskId) ?? 0) + 1;
      versions.set(taskId, version);
      return version;
    },
    isCurrent(taskId: string, version: number): boolean {
      return versions.get(taskId) === version;
    },
  };
}
