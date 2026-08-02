import { describe, expect, it } from "vitest";

import { ACTIVE_TASK_STATES, createTaskRefreshGuard, isActiveTask } from "../src/taskPolling";

describe("task polling freshness", () => {
  it("treats only unfinished task states as pollable", () => {
    expect(ACTIVE_TASK_STATES).toEqual(["queued", "submitting", "submitted", "downloading"]);
    expect(isActiveTask({ state: "uncertain" })).toBe(false);
    expect(isActiveTask({ state: "submitted" })).toBe(true);
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
