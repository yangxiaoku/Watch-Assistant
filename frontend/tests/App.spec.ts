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

  it("passes a disabled organization-plan capability to the library workbench", async () => {
    window.history.replaceState({}, "", "/library");
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };

    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({
      status: "ok",
      release: "test",
      push_supported: false,
      organization_plan_enabled: false,
      organization_execution_enabled: false,
      strm_capabilities: { full: true, incremental: true, cleanup: true, playback: false, playback_contract_verified: false },
    });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();
    vi.spyOn(ApiClient.prototype, "libraries").mockResolvedValue({ items: [library], next_cursor: null });
    vi.spyOn(ApiClient.prototype, "libraryMedia").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(ApiClient.prototype, "strmManifest").mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 });
    vi.spyOn(ApiClient.prototype, "strmOperations").mockResolvedValue({ items: [], next_cursor: null });

    const wrapper = mount(App);
    await flushPromises();

    const organizationButton = wrapper.findAll("button").find((button) => button.text().includes("生成整理预览"));
    expect(organizationButton?.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("自动整理计划未启用");
    wrapper.unmount();
  });
});
