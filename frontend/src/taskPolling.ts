import type { TaskResponse } from "./types";

export const ACTIVE_TASK_STATES = ["queued", "submitting", "submitted", "downloading"] as const;

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
