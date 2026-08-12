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
  requires_web_approval: false,
  high_risk_action_threshold: 10,
  alias: null,
  source_names: ["Movie.One.2024.mkv"],
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

    expect(wrapper.get(".organization-plan-row-files-name").text()).toBe("Movie.One.2024.mkv");
    expect(wrapper.get(".organization-preview h2").text()).toBe("Movie.One.2024.mkv");
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

    expect(wrapper.get(".organization-plan-badge").text()).toContain("已失效");
    expect(wrapper.find(".organization-actions").exists()).toBe(false);
    expect(wrapper.find(".organization-diagnostics").exists()).toBe(true);
  });

  it("loads a safe cursor page and sends the current revision for local actions", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    // 默认视图(null)不传 status,由后端返回活跃计划(待确认 + 已确认)
    expect(api.organizationPlans).toHaveBeenCalledWith({ cursor: undefined, limit: 20 });
    expect(wrapper.text()).toContain("整理");
    expect(wrapper.text()).toContain("版本 4");
    expect(wrapper.text()).not.toContain("pickcode");
    expect(wrapper.text()).not.toContain("/private/");

    await wrapper.findAll(".primary-button").find((button) => button.text().includes("开始整理"))!.trigger("click");
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
    expect(api.organizationPlans).toHaveBeenNthCalledWith(2, { cursor: 12, limit: 20 });
    expect(wrapper.findAll(".organization-plan-row")).toHaveLength(2);
    expect(wrapper.get(".organization-preview h2").text()).toBe("Movie.One.2024.mkv");

    await wrapper.findAll(".organization-plan-row")[1].trigger("click");
    await flushPromises();
    expect(operationFor).toHaveBeenCalledWith("plan-local-2");
    expect(wrapper.get(".organization-preview h2").text()).toBe("Movie.One.2024.mkv");
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

  it("disables execution buttons while an operation is active", async () => {
    const api = makeApi({
      organizationPlanOperation: vi.fn().mockResolvedValue({
        operation_id: "op-running",
        plan_id: plan.plan_id,
        status: "organizing" as const,
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    const executeButton = wrapper.findAll(".primary-button").find((button) => button.text().includes("开始整理"));
    expect(executeButton).toBeDefined();
    expect(executeButton!.attributes("disabled")).toBeDefined();
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

    expect(wrapper.get(".organization-plan-badge").text()).toContain("已确认");
    expect(wrapper.text()).not.toContain("待识别");
  });

  it("refreshes after a stale revision and never reports success", async () => {
    const api = makeApi({
      confirmAndQueueOrganizationOperation: vi.fn().mockRejectedValue(new ApiError("计划版本已变化，请刷新后重试", 409, "stale_revision")),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();
    await wrapper.findAll(".primary-button").find((button) => button.text().includes("开始整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);

    expect(api.organizationPlans).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("计划版本已变化，已刷新当前列表");
    expect(wrapper.text()).not.toContain("确认计划");
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
    expect(wrapper.text()).toContain("跳过 1 个未参与批量提交的计划");
  });

  

  it("queues a planned operation only when execution is enabled", async () => {
    const planned = { ...plan, status: "planned" as const };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [planned], next_cursor: null }),
      queueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-1", plan_id: planned.plan_id, status: "planned", revision: 1, attempts: 0, error_code: null }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    const operationButton = wrapper.findAll("button").find((button) => button.text().includes("开始整理"));
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
    expect(wrapper.findAll("button").some((button) => button.text().includes("开始整理"))).toBe(false);
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

    await wrapper.findAll("button").find((button) => button.text().includes("开始整理"))!.trigger("click");
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

    expect(wrapper.findAll("button").some((button) => button.text().includes("开始整理"))).toBe(false);
    await wrapper.get("#organization-candidate-query").setValue("The Office 2005");
    await wrapper.get(".organization-candidate-search").trigger("submit");
    await flushPromises();
    expect(api.searchOrganizationCandidates).toHaveBeenCalledWith("plan-local-1", 4, "The Office 2005", 0);

    await wrapper.get(".organization-candidate").trigger("click");
    await flushPromises();
    expect(api.selectOrganizationCandidate).toHaveBeenCalledWith("plan-local-1", 5, "source-1", 42);
    expect(wrapper.text()).toContain("可自动整理");
    await wrapper.findAll("button").find((button) => button.text().includes("开始整理"))!.trigger("click");
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

  it("shows a timeout notice when the operation poll exceeds the attempt window", async () => {
    vi.useFakeTimers();
    try {
      const organizing = {
        operation_id: "op-slow",
        plan_id: plan.plan_id,
        status: "organizing" as const,
        revision: 1,
        attempts: 1,
        error_code: null,
        cancel_requested: false,
      };
      const api = makeApi({
        organizationPlanOperation: vi.fn().mockResolvedValue(organizing),
        organizationOperation: vi.fn().mockResolvedValue(organizing), // 一直不进入终态
      });
      const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
      await flushPromises();

      expect(wrapper.text()).toContain("整理操作：执行中");
      // 60 次尝试 × 1s 后仍无终态,给出超时提示而不是静默结束
      await vi.advanceTimersByTimeAsync(60_000);
      await flushPromises();

      expect(wrapper.text()).toContain("等待超时，后台可能仍在执行，请手动刷新查看");
    } finally {
      vi.useRealTimers();
    }
  });

  it("polls accepted batch operations until they reach a terminal state", async () => {
    vi.useFakeTimers();
    try {
      const planned = {
        operation_id: "op-1",
        plan_id: plan.plan_id,
        status: "planned" as const,
        revision: 1,
        attempts: 0,
        error_code: null,
        cancel_requested: false,
      };
      const organized = { ...planned, status: "organized" as const, attempts: 1 };
      const api = makeApi({
        confirmAndQueueOrganizationOperations: vi.fn().mockResolvedValue({ items: [planned] }),
        organizationOperation: vi.fn().mockResolvedValue(organized),
      });
      const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
      await flushPromises();

      await wrapper.get(".organization-batch-action").trigger("click");
      await confirmRiskyAction(wrapper);
      expect(wrapper.text()).toContain("已确认并提交 1 个整理计划");

      await vi.advanceTimersByTimeAsync(1_000);
      await flushPromises();

      expect(api.organizationOperation).toHaveBeenCalledWith("op-1");
      expect(wrapper.text()).toContain("批量整理已完成，共 1 个计划全部完成");
    } finally {
      vi.useRealTimers();
    }
  });

  it("refreshes the plan row after confirm-and-queue so a second action uses the new revision", async () => {
    const api = makeApi({
      // 首次确认并整理后操作到达终态(organized),执行按钮恢复可用,随后可用新版本再次执行
      confirmAndQueueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-confirm", plan_id: plan.plan_id, status: "organized", revision: 1, attempts: 1, error_code: null, cancel_requested: false }),
      queueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-queued", plan_id: plan.plan_id, status: "organized", revision: 1, attempts: 1, error_code: null, cancel_requested: false }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    await wrapper.findAll(".primary-button").find((button) => button.text().includes("开始整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);

    // 确认后行内数据同步为 planned + 新版本,随后可直接执行,不会再用旧版本
    expect(api.confirmAndQueueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 4);
    const executeButton = wrapper.findAll("button").find((button) => button.text().includes("开始整理"));
    expect(executeButton).toBeDefined();
    await executeButton!.trigger("click");
    await confirmRiskyAction(wrapper);
    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 5);
  });

  it("guides the user to connection settings when candidate search is unavailable", async () => {
    const api = makeApi({
      searchOrganizationCandidates: vi.fn().mockRejectedValue(
        new ApiError("候选搜索暂不可用", 503, "candidate_search_unavailable"),
      ),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    await wrapper.get("#organization-candidate-query").setValue("The Office");
    await wrapper.get(".organization-candidate-search").trigger("submit");
    await flushPromises();

    expect(wrapper.get(".organization-candidate-search-guidance").text()).toContain("TMDB API Key");
    await wrapper.get(".organization-candidate-search-guidance .text-button").trigger("click");
    expect(wrapper.emitted("open-settings")).toEqual([["credentials"]]);
  });

  it("explains the missing execution capability instead of silently hiding actions", async () => {
    const api = makeApi(); // 计划 can_execute,但不传 executionSupported
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    const guidance = wrapper.get(".organization-execution-guidance");
    expect(guidance.text()).toContain("115 写契约验证");
    await wrapper.get(".organization-execution-guidance .text-button").trigger("click");
    expect(wrapper.emitted("open-settings")).toEqual([["organization"]]);
    // 可执行计划仍可做本地确认,只是不提供远端执行按钮
    expect(wrapper.findAll("button").some((button) => button.text().includes("开始整理"))).toBe(false);
    expect(wrapper.findAll("button").some((button) => button.text().includes("确认计划"))).toBe(true);
  });

  it("passes the Web approval workflow when queuing a high-risk plan", async () => {
    const planned = { ...plan, status: "planned" as const, action_count: 11, requires_web_approval: true };
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({ items: [planned], next_cursor: null }),
      createOrganizationApprovalWorkflow: vi.fn().mockResolvedValue({ id: "wf-high-risk" }),
      queueOrganizationOperation: vi.fn().mockResolvedValue({ operation_id: "op-2", plan_id: planned.plan_id, status: "planned", revision: 1, attempts: 0, error_code: null }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionSupported: true } });
    await flushPromises();

    const approvalButton = wrapper.findAll("button").find((button) => button.text().includes("申请 Web 人工审批"));
    expect(approvalButton).toBeDefined();
    await approvalButton!.trigger("click");
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("开始整理"))!.trigger("click");
    await confirmRiskyAction(wrapper);

    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 4, "wf-high-risk");
  });
});
