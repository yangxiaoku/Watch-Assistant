import { describe, expect, it } from "vitest";

import {
  capabilityStatusPresentation,
  libraryScanStatusLabel,
  organizationOperationStatusLabel,
  strmOperationNextStep,
  strmOperationStatusLabel,
  taskStatusPresentation,
  workflowStatusPresentation,
} from "../src/statusCatalog";

describe("statusCatalog", () => {
  it("uses the same actionable states for workbench capabilities", () => {
    expect(capabilityStatusPresentation({ enabled: true, reason_code: null, reason_zh: "可执行" })).toMatchObject({ label: "可用", tone: "success" });
    expect(capabilityStatusPresentation({ enabled: false, reason_code: "strm_full_disabled", reason_zh: "STRM 全量生成未启用。" })).toMatchObject({ label: "需配置", nextStep: "STRM 全量生成未启用。" });
    expect(capabilityStatusPresentation({ enabled: false, reason_code: "capability_unknown", reason_zh: "当前未读取能力状态。" })).toMatchObject({ label: "状态待确认" });
  });

  it("maps task and workflow states to actionable Chinese copy", () => {
    expect(taskStatusPresentation("queued")).toMatchObject({ label: "排队中", tone: "info" });
    expect(taskStatusPresentation("uncertain")).toMatchObject({ label: "结果待确认", nextStep: expect.stringContaining("只读核对") });
    expect(workflowStatusPresentation("partial")).toMatchObject({ label: "部分完成", nextStep: expect.stringContaining("未完成阶段") });
  });

  it("distinguishes partial STRM success and blocks blind timeout retry", () => {
    const partial = { status: "succeeded" as const, failed: 1, skipped: 0 };
    const timeout = { status: "timeout" as const, failed: 0, skipped: 0 };

    expect(strmOperationStatusLabel(partial)).toBe("部分完成");
    expect(strmOperationNextStep(partial)).toContain("失败统计");
    expect(strmOperationNextStep(timeout)).toContain("不要恢复执行");
  });

  it("does not describe an incomplete scan as complete", () => {
    expect(libraryScanStatusLabel({ state: "completed", complete: false })).toBe("未完成");
    expect(libraryScanStatusLabel(null)).toBe("未扫描");
  });

  it("does not label a planned operation as queued without execution capability", () => {
    expect(organizationOperationStatusLabel("planned", false)).toBe("等待执行能力（未执行）");
    expect(organizationOperationStatusLabel("planned", true)).toBe("已排队");
  });
});
