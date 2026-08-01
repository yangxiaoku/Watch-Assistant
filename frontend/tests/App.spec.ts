import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { ApiClient } from "../src/api";

describe("App capability wiring", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it("passes the enabled organization execution capability to the workbench", async () => {
    window.history.replaceState({}, "", "/organization-plans");
    const plan = {
      plan_id: "plan-local-1",
      plan_hash: "a".repeat(64),
      status: "needs_review",
      revision: 1,
      expires_at: "2026-08-01T00:00:00Z",
      source_count: 1,
      action_count: 1,
      precondition_count: 1,
      alias: null,
      can_execute: true,
      candidates: [],
    };

    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({
      status: "ok",
      release: "test",
      push_supported: false,
      organization_plan_enabled: true,
      organization_execution_enabled: true,
    });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();
    vi.spyOn(ApiClient.prototype, "organizationPlans").mockResolvedValue({ items: [plan], next_cursor: null });
    vi.spyOn(ApiClient.prototype, "organizationPlanOperation").mockResolvedValue(null);

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("确认并开始整理");
    expect(wrapper.text()).not.toContain("这里只改变本地计划状态，不会执行远端操作。");
    wrapper.unmount();
  });
});
