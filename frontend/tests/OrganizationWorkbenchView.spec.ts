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
  precondition_count: 2,
  alias: null,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const response: OrganizationPlanListResponse = { items: [plan], next_cursor: null };
  return {
    organizationPlans: vi.fn().mockResolvedValue(response),
    confirmOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, status: "planned", revision: 5 }),
    ignoreOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, status: "ignored", revision: 5 }),
    aliasOrganizationPlan: vi.fn().mockResolvedValue({ ...plan, alias: "本地标签", revision: 5 }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("OrganizationWorkbenchView", () => {
  it("does not request or render the workbench when disabled", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, enabled: false } });
    await flushPromises();

    expect(api.organizationPlans).not.toHaveBeenCalled();
    expect(wrapper.find(".organization-workbench").exists()).toBe(false);
  });

  it("hides mutation controls for invalidated plans", async () => {
    const api = makeApi({
      organizationPlans: vi.fn().mockResolvedValue({
        items: [{ ...plan, status: "invalidated" }],
        next_cursor: null,
      }),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    expect(wrapper.get(".organization-status").text()).toContain("已失效");
    expect(wrapper.find(".organization-actions").exists()).toBe(false);
    expect(wrapper.find(".organization-alias").exists()).toBe(false);
  });

  it("loads a safe cursor page and sends the current revision for local actions", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();

    expect(api.organizationPlans).toHaveBeenCalledWith({ status: "needs_review", cursor: undefined, limit: 20 });
    expect(wrapper.text()).toContain("整理计划工作台");
    expect(wrapper.text()).toContain("版本 4");
    expect(wrapper.text()).not.toContain("pickcode");
    expect(wrapper.text()).not.toContain("/private/");

    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();
    expect(api.confirmOrganizationPlan).toHaveBeenCalledWith("plan-local-1", 4);
    expect(wrapper.text()).toContain("已确认本地计划");
  });

  it("refreshes after a stale revision and never reports success", async () => {
    const api = makeApi({
      confirmOrganizationPlan: vi.fn().mockRejectedValue(new ApiError("计划版本已变化，请刷新后重试", 409, "stale_revision")),
    });
    const wrapper = mount(OrganizationWorkbenchView, { props: { api } });
    await flushPromises();
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();

    expect(api.organizationPlans).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("计划版本已变化，已刷新当前列表");
    expect(wrapper.text()).not.toContain("已确认本地计划，未执行远端写操作");
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
    const wrapper = mount(OrganizationWorkbenchView, { props: { api, executionEnabled: true } });
    await flushPromises();

    const operationButton = wrapper.findAll("button").find((button) => button.text().includes("提交远端整理"));
    expect(operationButton).toBeDefined();
    await operationButton!.trigger("click");
    await flushPromises();
    expect(api.queueOrganizationOperation).toHaveBeenCalledWith("plan-local-1", 4);
    expect(wrapper.text()).toContain("整理操作已排队");
  });
});
