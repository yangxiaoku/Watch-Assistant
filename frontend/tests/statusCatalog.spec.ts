import { describe, expect, it } from "vitest";

import {
  libraryScanStatusLabel,
  strmOperationNextStep,
  strmOperationStatusLabel,
  taskStatusPresentation,
  workflowStatusPresentation,
} from "../src/statusCatalog";

describe("statusCatalog", () => {
  it("maps task and workflow states to actionable Chinese copy", () => {
    expect(taskStatusPresentation("queued")).toMatchObject({ label: "排队中", tone: "info" });
    expect(taskStatusPresentation("uncertain")).toMatchObject({ label: "结果待确认", nextStep: expect.stringContaining("只读核对") });
    expect(workflowStatusPresentation("partial")).toMatchObject({ label: "部分成功", nextStep: expect.stringContaining("未完成阶段") });
  });

  it("distinguishes partial STRM success and blocks blind timeout retry", () => {
    const partial = { status: "succeeded" as const, failed: 1, skipped: 0 };
    const timeout = { status: "timeout" as const, failed: 0, skipped: 0 };

    expect(strmOperationStatusLabel(partial)).toBe("部分成功");
    expect(strmOperationNextStep(timeout)).toContain("不要恢复执行");
  });

  it("does not describe an incomplete scan as complete", () => {
    expect(libraryScanStatusLabel({ state: "completed", complete: false })).toBe("未完成");
    expect(libraryScanStatusLabel(null)).toBe("未扫描");
  });
});
