import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import type { OrganizationPlanListResponse, OrganizationPlanSummary } from "../src/types";
import OrganizationWorkbenchView from "../src/views/OrganizationWorkbenchView.vue";

const plan: OrganizationPlanSummary = {
  plan_id: "plan-local-1",
  plan_hash: "a".repeat(64),
  status: "needs_review",
  revision: 4,
  expires_at: "2026-08-01T00:00:00Z",
  source_count: 2,
  action_count: 1,
  precondition_count: 1,
  executable_action_count: 1,
  review_action_count: 0,
  can_execute: true,
  alias: null,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const response: OrganizationPlanListResponse = { items: [plan], next_cursor: null };
  return {
    organizationPlans: vi.fn().mockResolvedValue(response),
    confirmOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, status: "planned", revision: 5 }),
    confirmAndQueueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-confirm", plan_id: plan.plan_id, status: "planned", revision: 1, attempts: 0, error_code: null, cancel_requested: false }),
    confirmAndQueueOrganizationOperations: vi.fn().mockResolvedValue({ items: [] }),
    ignoreOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, status: "ignored", revision: 5 }),
    aliasOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, alias: "本地标签", revision: 5 }),
    ...overrides,
  } as unknown as ApiClient;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

async function confirmRiskyAction(wrapper: ReturnType<typeof mount>) {
  // Execution confirmations are a single confirm (details shown, no extra
  // acknowledgment checkbox gate).
  await wrapper.get(".confirm-dialog-actions button:last-child").trigger("click");
  await flushPromises();
}

describe("OrganizationWorkbenchView", () => {
  it("does not request or render the workbench when disabled", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, enabled: false } });
    await flushPromises();

    expect(api.organizationPlans).not.toHaveBeenCalled();
    expect(wrapper.find(".organization-workbench").exists()).toBe(false);
  });

  it("uses a safe local identifier when a plan has no alias", async () => {
    const wrapper = mount(OrganizationWorkbenchView, { props: { api: makeApi() } });
    await flushPromises();

    expect(wrapper.get(".organization-plan-row strong").text()).toBe("计划 plan-local-1");
    expect(wrapper.get(".organization-preview h2").text()).toBe("计划 plan-local-1");
  });

  it("hides mutation controls for invalidated plans", async () => {
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({
        items: [{ ...plan, status: "invalidated" }],
        next_cursor: null,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    expect(wrapper.get(".organization-status").text()).toContain("已失效");
    expect(wrapper.find(".organization-actions").exists()).toBe(false);
    expect(wrapper.find(".organization-alias").exists()).toBe(false);
  });

  it("loads a safe cursor page and sends the current revision for local actions", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    expect(api.organizationPlans).toHaveBeenCalledWith({ status: "needs_review", cursor: undefined, limit: 20 });
    expect(wrapper.text()).toContain("整理计划工作台");
    expect(wrapper.text()).toContain("版本 4");
    expect(wrapper.text()).not.toContain("pickcode");
    expect(wrapper.text()).not.toContain("/private/");

    await wrapper.findAll(".primary-button").find((button) => button.text().includes("确认并开始整理"))!.trigger("click");
    expect(wrapper.get(".confirm-dialog").text()).toContain("预计移动");
    await confirmRiskyAction(wrapper);
    expect(api.confirmAndQueueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 4);
    expect(wrapper.text()).toContain("整理已提交，后台正在执行");
  });

  it("appends cursor pages and loads operation details for the selected plan", async () => {
    const second = { ...plan, plan_id: "plan-local-2", alias: "第二计划", revision: 7 };
    const operationFor = vi.fn().mockImplementation(async (planId: string) => ({
      operation_id: `operation-${planId}`,
      plan_id: planId,
      status: "organized" as const,
      revision: 1,
      attempts: 1,
      error_code: null,
      cancel_requested: false,
    }));
    const api = makeApi({
      organizationPlans: vi.fn()
        .mockResolvedValueOnce({ items: [plan], next_cursor: 12 })
        .mockResolvedValueOnce({ items: [second], next_cursor: null }),
      organizationPlanOperation: operationFor,
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    await wrapper.get(".organization-more").trigger("click");
    await flushPromises();
    expect(api.organizationPlans).toHaveBeenNthCalledWith(2, { status: "needs_review", cursor: 12, limit: 20 });
    expect(wrapper.findAll(".organization-plan-row")).toHaveLength(2);
    expect(wrapper.get(".organization-preview h2").text()).toBe("计划 plan-local-1");

    await wrapper.findAll(".organization-plan-row")[1].trigger("click");
    await flushPromises();
    expect(operationFor).toHaveBeenCalledWith("plan-local-2");
    expect(wrapper.get(".organization-preview h2").text()).toBe("第二计划");
    expect(wrapper.text()).toContain("整理操作：已完成");
  });

  it("resumes a persisted organizing operation after loading", async () => {
    vi.useFakeTimers();
    try {
      const organizing = {
        operation_id: "op-running",
        plan_id: plan.plan_id,
        status: "organizing" as const,
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      };
      const organized = { ...organizing, status: "organized" as const };
      const organizationOperation = vi.fn().mockResolvedValue(organized);
      const api = makeApi({
        organizationPlanOperation: vi.fn().mockResolvedValue(organizing),
        organizationOperation,
      });
      const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
      await flushPromises();

      expect(wrapper.text()).toContain("整理操作：执行中");
      await vi.advanceTimersByTimeAsync(1_000);
      await flushPromises();

      expect(organizationOperation).toHaveBeenCalledWith("op-running");
      expect(wrapper.text()).toContain("整理操作：已完成");
      expect(wrapper.text()).toContain("整理已完成");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a structured error when a persisted operation cannot be loaded", async () => {
    const operationFor = vi.fn()
      .mockRejectedValueOnce(new ApiError("整理操作状态服务暂不可用，请稍后重试", 503, "organization_operation_unavailable"))
      .mockResolvedValueOnce({
        operation_id: "op-recovered",
        plan_id: plan.plan_id,
        status: "organized",
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      });
    const api = makeApi({ organizationPlanOperation: operationFor });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    expect(wrapper.get(".error-strip").text()).toContain("整理操作状态服务暂不可用，请稍后重试");
    await wrapper.get(".error-strip .text-button").trigger("click");
    await flushPromises();
    expect(operationFor).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".error-strip").exists()).toBe(false);
  });

  it("keeps an operation polling error visible with a retry action", async () => {
    vi.useFakeTimers();
    try {
      const organizing = {
        operation_id: "op-poll-error",
        plan_id: plan.plan_id,
        status: "organizing" as const,
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      };
      const api = makeApi({
        organizationPlanOperation: vi.fn().mockResolvedValue(organizing),
        organizationOperation: vi.fn().mockRejectedValue(new ApiError("整理状态暂时无法更新，请稍后重试", 503, "organization_operation_unavailable")),
      });
      const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1_000);
      await flushPromises();

      expect(wrapper.get(".error-strip").text()).toContain("整理状态暂时无法更新，请稍后重试");
      expect(wrapper.get(".error-strip .text-button").text()).toBe("重试");
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores an older plan response after the status filter changes", async () => {
    const firstResponse = deferred<OrganizationPlanListResponse>();
    const secondResponse = deferred<OrganizationPlanListResponse>();
    const api = makeApi({
      organizationPlans: vi.fn()
        .mockReturnValueOnce(firstResponse.promise)
        .mockReturnValueOnce(secondResponse.promise),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });

    await wrapper.findAll(".organization-tabs button")[1].trigger("click");
    secondResponse.resolve({ items: [{ ...plan, status: "planned" }], next_cursor: null });
    await flushPromises();
    firstResponse.resolve({ items: [plan], next_cursor: null });
    await flushPromises();

    expect(wrapper.get(".organization-status").text()).toContain("已确认");
    expect(wrapper.text()).not.toContain("待确认当前计划已选中");
  });

  it("refreshes after a stale revision and never reports success", async () => {
    const api = makeApi({
      confirmAndQueueOrganizationOperation: vi.fn().mockRejectedValue(new ApiError("计划版本已变化，请刷新后重试", 409, "stale_revision")),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();
    await wrapper.findAll(".primary-button").find((button) => button.text().includes("确认并开始整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);

    expect(api.organizationPlans).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("计划版本已变化，已刷新当前列表");
    expect(wrapper.text()).not.toContain("已确认本地计划，未执行远端写操作");
  });

  it("confirms and queues all visible review plans in one action", async () => {
    const second = {
      ...plan,
      plan_id: "plan-local-2",
      revision: 2,
      executable_action_count: 0,
      review_action_count: 1,
      can_execute: false,
    };
    const api = makeApi({
      organizationPlans: vi.fn()
        .mockResolvedValueOnce({ items: [plan, second], next_cursor: null })
        .mockResolvedValueOnce({ items: [], next_cursor: null }),
      confirmAndQueueOrganizationOperations: vi.fn().mockResolvedValue({
        items: [
          { plan_id: plan.plan_id, operation_id: "op-1", status: "planned", revision: 1, attempts: 0, error_code: null, message: "整理操作已排队" },
        ],
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    await wrapper.get(".organization-batch-action").trigger("click");
    await confirmRiskyAction(wrapper);

    expect(api.confirmAndQueueOrganizationOperations).toHaveBeenCalledWith([
      { planId: plan.plan_id, expectedRevision: plan.revision },
    ]);
    expect(wrapper.text()).toContain("已确认并提交 1 个整理计划");
    expect(wrapper.text()).toContain("跳过 1 个待搜索或复核计划");
  });

  it("passes the selected revision when saving a local alias", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();
    const input = wrapper.get("#organization-alias-input");
    await input.setValue("本地标签");
    await wrapper.get(".organization-alias").trigger("submit");
    await flushPromises();

    expect(api.aliasOrganizationPlan).toHaveBeenCalledWith("plan-local-1", "本地标签", 4);
  });

  it("queues a planned operation only when execution is enabled", async () => {
    const planned = { ...plan, status: "planned" as const };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [planned], next_cursor: null }),
      queueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-1", plan_id: planned.plan_id, status: "planned", revision: 1, attempts: 0, error_code: null }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    const operationButton = wrapper.findAll("button").find((button) => button.text().includes("立即整理"));
    expect(operationButton).toBeDefined();
    await operationButton!.trigger("click");
    await confirmRiskyAction(wrapper);
    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 4);
    expect(wrapper.text()).toContain("整理已提交，后台正在执行");
  });

  it("labels a legacy planned operation as not executed when capability is unavailable", async () => {
    const planned = { ...plan, status: "planned" as const };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [planned], next_cursor: null }),
      organizationPlanOperation: vi.fn().mockResolvedValue({
        operation_id: "op-legacy",
        plan_id: planned.plan_id,
        status: "planned",
        revision: 1,
        attempts: 0,
        error_code: null,
        cancel_requested: false,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("整理操作：等待执行能力（未执行）");
    expect(wrapper.text()).toContain("本次操作尚未执行");
    expect(wrapper.text()).not.toContain("整理操作：已排队");
    expect(wrapper.findAll("button").some((button) => button.text().includes("立即整理"))).toBe(false);
  });

  it("does not queue immediately after selecting a candidate", async () => {
    const reviewPlan = {
      ...plan,
      candidates: [{
        source_object_id: "source-1",
        tmdb_id: 42,
        title: "The Office",
        media_type: "movie" as const,
        release_year: 2005,
      }],
    };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [reviewPlan], next_cursor: null }),
      selectOrganizationCandidate: vi.fn().mockResolvedValue({
        ...reviewPlan,
        status: "planned",
        revision: 5,
        candidates: [],
        executable_action_count: 1,
        review_action_count: 0,
        can_execute: true,
      }),
      queueOrganizationOperation: vi.fn().mockResolvedValue({
        operation_id: "op-after-selection",
        plan_id: plan.plan_id,
        status: "organized",
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    await wrapper.get(".organization-candidate").trigger("click");
    await flushPromises();

    expect(api.selectOrganizationCandidate).toHaveBeenCalledWith("plan-local-1", 4, "source-1", 42);
    expect(api.queueOrganizationOperation).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("已生成可执行计划，请确认后开始整理");

    await wrapper.findAll("button").find((button) => button.text().includes("立即整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);
    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 5);
    expect(wrapper.text()).toContain("整理已完成");
  });

  it("keeps a no-candidate plan review-only until manual search and selection create a move", async () => {
    const reviewOnly = {
      ...plan,
      candidates: [],
      executable_action_count: 0,
      review_action_count: 1,
      can_execute: false,
    };
    const searched = {
      ...reviewOnly,
      revision: 5,
      candidates: [{
        source_object_id: "source-1",
        tmdb_id: 42,
        title: "The Office",
        media_type: "movie" as const,
        release_year: 2005,
      }],
    };
    const planned = {
      ...searched,
      status: "planned" as const,
      revision: 6,
      candidates: [],
      executable_action_count: 1,
      review_action_count: 0,
      can_execute: true,
    };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [reviewOnly], next_cursor: null }),
      searchOrganizationCandidates: vi.fn().mockResolvedValue(searched),
      selectOrganizationCandidate: vi.fn().mockResolvedValue(planned),
      queueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-manual", plan_id: plan.plan_id, status: "organized", revision: 1, attempts: 1, error_code: null, cancel_requested: false }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    expect(wrapper.findAll("button").some((button) => button.text().includes("确认并开始整理"))).toBe(false);
    await wrapper.get("#organization-candidate-query").setValue("The Office 2005");
    await wrapper.get(".organization-candidate-search").trigger("submit");
    await flushPromises();
    expect(api.searchOrganizationCandidates).toHaveBeenCalledWith("plan-local-1", 4, "The Office 2005", 0);

    await wrapper.get(".organization-candidate").trigger("click");
    await flushPromises();
    expect(api.selectOrganizationCandidate).toHaveBeenCalledWith("plan-local-1", 5, "source-1", 42);
    expect(wrapper.text()).toContain("可执行移动");
    await wrapper.findAll("button").find((button) => button.text().includes("立即整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);
    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 6);
  });

  it("shows the durable operation failure instead of a generic page error", async () => {
    const invalidated = { ...plan, status: "invalidated" as const, revision: 5 };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [invalidated], next_cursor: null }),
      organizationPlanOperation: vi.fn().mockResolvedValue({
        operation_id: "op-stale",
        plan_id: invalidated.plan_id,
        status: "failed",
        revision: 3,
        attempts: 1,
        error_code: "plan_prerequisites_changed",
        cancel_requested: false,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    expect(wrapper.text()).toContain("整理操作：已失败");
    expect(wrapper.text()).toContain("扫描快照已更新，原计划已失效");
    expect(wrapper.text()).not.toContain("本次操作未完成，当前页面没有更新");
  });

  it("renders structured execution blockers with a Chinese next step", async () => {
    const blockedPlan = {
      ...plan,
      status: "planned" as const,
      can_execute: false,
      execution_blockers: [
        {
          kind: "snapshot_changed" as const,
          code: "source_snapshot_changed",
          message_zh: "来源快照已变化或无法核对，原计划不能执行。",
          next_step_zh: "重新扫描并生成新的整理计划。",
        },
      ],
    };
    const api = makeApi({ organizationPlans: vi.fn().mockResolvedValue({ items: [blockedPlan], next_cursor: null }) });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    expect(wrapper.get(".organization-execution-blockers").text()).toContain("来源快照已变化");
    expect(wrapper.get(".organization-execution-blockers").text()).toContain("下一步：重新扫描");
    expect(wrapper.findAll("button").some((button) => button.text().includes("立即整理"))).toBe(false);
  });
});
