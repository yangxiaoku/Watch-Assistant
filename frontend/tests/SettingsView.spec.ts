import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import SettingsView from "../src/views/SettingsView.vue";

const overview = {
  revision: "rev-1",
  version: "0.4.0",
  uptime_seconds: 90061,
  database_size_bytes: 3 * 1024 * 1024,
  components: [
    { name: "数据库", status: "ok" as const, detail: "WAL" },
    { name: "PanSou", status: "degraded" as const, detail: "缓存可用" },
  ],
};
const logging = {
  revision: "rev-1",
  level: "info" as const,
  retention_days: 30,
  capacity_mb: 512,
};
const p115 = {
  enabled: true,
  readiness: "ready" as const,
  cookie_source: "file" as const,
  cookie_structure: "valid" as const,
  cookie_synced_at: "2026-07-25T02:00:00Z",
  capabilities: { magnet: true, share: false },
};
const logs = {
  items: [{ id: "log-1", timestamp: "2026-07-25T02:00:00Z", level: "info" as const, category: "system", message: "服务已启动" }],
  page: 1,
  page_size: 20,
  total: 1,
  total_pages: 1,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    settingsOverview: vi.fn().mockResolvedValue(overview),
    loggingSettings: vi.fn().mockResolvedValue(logging),
    updateLoggingSettings: vi.fn().mockResolvedValue({ ...logging, revision: "rev-2" }),
    p115Settings: vi.fn().mockResolvedValue(p115),
    validateP115Cookie: vi.fn().mockResolvedValue(p115),
    logs: vi.fn().mockResolvedValue(logs),
    ...overrides,
  } as unknown as ApiClient;
}

describe("SettingsView", () => {
  it("renders overview and p115 status without a cookie input", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("0.4.0");
    expect(wrapper.text()).toContain("1 天 1 小时");
    expect(wrapper.text()).toContain("3.0 MB");
    expect(wrapper.find("input[type=password]").exists()).toBe(false);

    const p115Button = wrapper.findAll("button").find((button) => button.text().includes("115 推送"));
    expect(p115Button).toBeDefined();
    await p115Button?.trigger("click");
    expect(wrapper.text()).toContain("Cookie 来源");
    expect(wrapper.text()).toContain("115 分享转存");
    expect(wrapper.text()).toContain("未启用");
    await wrapper.get("button.secondary-button").trigger("click");
    expect(api.validateP115Cookie).toHaveBeenCalledOnce();
  });

  it("loads filtered logs and shows a save bar only after editing", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    const logsButton = wrapper.findAll("button").find((button) => button.text().includes("日志"));
    await logsButton?.trigger("click");
    await flushPromises();

    expect(api.logs).toHaveBeenCalledWith({ page: 1, pageSize: 20, level: undefined, category: undefined });
    expect(wrapper.text()).toContain("服务已启动");
    expect(wrapper.find(".settings-save-bar").exists()).toBe(false);

    const selects = wrapper.findAll("select");
    await selects[1].setValue("error");
    await flushPromises();
    expect(api.logs).toHaveBeenLastCalledWith({ page: 1, pageSize: 20, level: "error", category: undefined });
    expect(wrapper.find(".settings-save-bar").exists()).toBe(false);

    await wrapper.get(".logging-settings select").setValue("warning");
    expect(wrapper.find(".settings-save-bar").exists()).toBe(true);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(api.updateLoggingSettings).toHaveBeenCalledWith({ revision: "rev-1", level: "warning", retention_days: 30, capacity_mb: 512 });
    expect(wrapper.find(".settings-save-bar").exists()).toBe(false);
  });

  it("offers reload after a revision conflict", async () => {
    const api = makeApi({
      updateLoggingSettings: vi.fn().mockRejectedValue(new ApiError("conflict", 409)),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    const logsButton = wrapper.findAll("button").find((button) => button.text().includes("日志"));
    await logsButton?.trigger("click");
    await flushPromises();
    await wrapper.get(".logging-settings select").setValue("debug");
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("设置已被其他请求修改");
    expect(wrapper.text()).toContain("重新加载");
    expect(wrapper.find("input[type=password]").exists()).toBe(false);
  });
});
