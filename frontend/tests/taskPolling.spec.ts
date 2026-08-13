import { describe, expect, it } from "vitest";

import { ACTIVE_TASK_STATES, createTaskRefreshGuard, isActiveTask } from "../src/taskPolling";

describe("task polling freshness", () => {
  it("treats only high-frequency states as pollable", () => {
    // 提交成功后的 submitted/downloading 由后端 watcher 分钟级推进,不再用
    // 2 秒前端轮询假装"正在处理";drawer 打开时刷新 + 只读核对兜底。
    expect(ACTIVE_TASK_STATES).toEqual(["queued", "submitting"]);
    expect(isActiveTask({ state: "uncertain" })).toBe(false);
    expect(isActiveTask({ state: "submitted" })).toBe(false);
    expect(isActiveTask({ state: "downloading" })).toBe(false);
  });

  it("invalidates a slow poll response after reconciliation updates the task", () => {
    const guard = createTaskRefreshGuard();
    const pollVersion = guard.begin("task-1");

    guard.invalidate("task-1");

    expect(guard.isCurrent("task-1", pollVersion)).toBe(false);
  });

  it("keeps only the newest overlapping poll response", () => {
    const guard = createTaskRefreshGuard();
    const first = guard.begin("task-1");
    const second = guard.begin("task-1");

    expect(guard.isCurrent("task-1", first)).toBe(false);
    expect(guard.isCurrent("task-1", second)).toBe(true);
  });
});
