import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import type { LogsResponse } from "../src/types";
import SettingsView from "../src/views/SettingsView.vue";

const overview = {
  release: "2026.07.25",
  uptime_seconds: 90061,
  database_size_bytes: 3 * 1024 * 1024,
  capabilities: { inspection: true, magnet: true, share: false },
};
const logging = {
  revision: 7,
  level: "INFO" as const,
  retention_days: 30,
  max_file_mb: 10,
};
const p115 = {
  enabled: true,
  ready: true,
  capabilities: { magnet: true, share: false },
  cookie: {
    source: "tgtodrive" as const,
    configured: true,
    structure_valid: true,
    sync_status: "success" as const,
    last_sync_at: "2026-07-25T02:00:00Z",
  },
  target_configured: true,
  max_concurrency: 1,
};

const firstLogs: LogsResponse = {
  items: [
    { id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO" as const, category: "system" as const, message: "服务已启动" },
    { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING" as const, category: "cache" as const, message: "缓存已刷新" },
  ],
  next_cursor: 20,
};
const contentPolicy = {
  hide_adult_media: true,
  hide_suspicious_resources: true,
  hide_low_quality_resources: true,
  blocked_keywords: [],
  revision: 3,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    settingsOverview: vi.fn().mockResolvedValue(overview),
    loggingSettings: vi.fn().mockResolvedValue(logging),
    updateLoggingSettings: vi.fn().mockResolvedValue({ ...logging, revision: 8 }),
    p115Settings: vi.fn().mockResolvedValue(p115),
    validateP115Cookie: vi.fn().mockResolvedValue({ status: "ready", checked_at: "2026-07-25T02:00:00Z" }),
    logs: vi.fn().mockResolvedValue(firstLogs),
    contentPolicy: vi.fn().mockResolvedValue(contentPolicy),
    updateContentPolicy: vi.fn().mockResolvedValue({ ...contentPolicy, revision: 4 }),
    ...overrides,
  } as unknown as ApiClient;
}

async function openLogs(wrapper: ReturnType<typeof mount>) {
  const button = wrapper.findAll("button").find((item) => item.text().includes("日志"));
  await button?.trigger("click");
  await flushPromises();
}

describe("SettingsView", () => {
  it("renders the real overview fields and p115 settings without a cookie input", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("2026.07.25");
    expect(wrapper.text()).toContain("1 天 1 小时");
    expect(wrapper.text()).toContain("3.0 MB");
    expect(wrapper.text()).toContain("内容检测");
    expect(wrapper.text()).toContain("115 分享转存");
    expect(wrapper.text()).toContain("未启用");
    expect(wrapper.text()).not.toContain("组件状态");
    expect(wrapper.text()).not.toContain("配置版本");
    expect(wrapper.find("input[type=password]").exists()).toBe(false);

    const p115Button = wrapper.findAll("button").find((button) => button.text().includes("115 推送"));
    await p115Button?.trigger("click");
    expect(wrapper.text()).toContain("Cookie 来源");
    expect(wrapper.text()).toContain("TgtoDrive");
    expect(wrapper.text()).toContain("结构正常");
  });

  it("loads the first cursor page and appends later pages with id de-duplication", async () => {
    const api = makeApi({
      logs: vi.fn()
        .mockResolvedValueOnce(firstLogs)
        .mockResolvedValueOnce({
          items: [
            { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING" as const, category: "cache" as const, message: "缓存已刷新（重复）" },
            { id: 3, timestamp: "2026-07-25T02:02:00Z", level: "ERROR" as const, category: "security" as const, message: "需要重新登录" },
          ],
          next_cursor: null,
        }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await openLogs(wrapper);

    expect(api.logs).toHaveBeenCalledWith({ limit: 20, cursor: undefined, category: undefined });
    expect(wrapper.find(".settings-pagination").text()).toContain("已加载 2 条");
    await wrapper.get(".settings-pagination button").trigger("click");
    await flushPromises();

    expect(api.logs).toHaveBeenLastCalledWith({ limit: 20, cursor: 20, category: undefined });
    expect(wrapper.findAll(".settings-log-item")).toHaveLength(3);
    expect(wrapper.text()).toContain("需要重新登录");
    expect(wrapper.find(".settings-pagination button").exists()).toBe(false);
  });

  it("invalidates an older category request before applying its response", async () => {
    let resolveFirst: ((value: LogsResponse) => void) | undefined;
    let resolveSecond: ((value: LogsResponse) => void) | undefined;
    const api = makeApi({
      logs: vi.fn()
        .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
        .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; })),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    const logsButton = wrapper.findAll("button").find((button) => button.text().includes("日志"));
    await logsButton?.trigger("click");
    const category = wrapper.get(".settings-filter-row select");
    await category.setValue("search");
    resolveSecond?.({ ...firstLogs, items: [{ ...firstLogs.items[0], id: 9, category: "search", message: "新分类响应" }], next_cursor: null });
    await flushPromises();
    resolveFirst?.({ ...firstLogs, items: [{ ...firstLogs.items[0], id: 1, message: "旧分类响应" }], next_cursor: null });
    await flushPromises();

    expect(wrapper.text()).toContain("新分类响应");
    expect(wrapper.text()).not.toContain("旧分类响应");
  });

  it("auto refreshes the first page without dropping loaded cursor pages", async () => {
    vi.useFakeTimers();
    const api = makeApi({
      logs: vi.fn()
        .mockResolvedValueOnce(firstLogs)
        .mockResolvedValueOnce({
          items: [{ id: 3, timestamp: "2026-07-25T02:02:00Z", level: "INFO" as const, category: "system" as const, message: "旧页" }],
          next_cursor: null,
        })
        .mockResolvedValueOnce({
          items: [{ id: 4, timestamp: "2026-07-25T02:03:00Z", level: "INFO" as const, category: "system" as const, message: "自动刷新" }],
          next_cursor: 20,
        }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await openLogs(wrapper);
    await wrapper.get(".settings-pagination button").trigger("click");
    await flushPromises();

    vi.advanceTimersByTime(5000);
    await flushPromises();

    expect(api.logs).toHaveBeenLastCalledWith({ limit: 20, cursor: undefined, category: undefined });
    expect(wrapper.findAll(".settings-log-item")).toHaveLength(4);
    expect(wrapper.text()).toContain("旧页");
    expect(wrapper.text()).toContain("自动刷新");
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("pauses auto refresh while hidden or outside the log section", async () => {
    vi.useFakeTimers();
    const logsMock = vi.fn().mockResolvedValue(firstLogs);
    const api = makeApi({ logs: logsMock });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await openLogs(wrapper);
    const callsAfterLoad = logsMock.mock.calls.length;

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock.mock.calls.length).toBe(callsAfterLoad);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const overviewButton = wrapper.findAll("button").find((button) => button.text().includes("概览"));
    await overviewButton?.trigger("click");
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock.mock.calls.length).toBe(callsAfterLoad);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("loads content policy controls and handles a revision conflict", async () => {
    const api = makeApi({
      updateContentPolicy: vi.fn().mockRejectedValue(new ApiError("conflict", 409)),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    const contentButton = wrapper.findAll("button").find((button) => button.text().includes("内容安全"));
    await contentButton?.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("过滤成人媒体");
    await wrapper.get(".settings-policy-row input").setValue(false);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("设置已被其他请求修改");
  });

  it("saves uppercase logging settings with numeric revision and clears a conflict after reload", async () => {
    const api = makeApi({
      updateLoggingSettings: vi.fn().mockRejectedValue(new ApiError("conflict", 409)),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await openLogs(wrapper);
    await wrapper.get(".logging-settings select").setValue("WARNING");
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("设置已被其他请求修改");

    await wrapper.get(".settings-save-error .text-button").trigger("click");
    await flushPromises();
    expect(wrapper.find(".settings-save-error").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("设置已被其他请求修改");
  });

  it.each([
    ["ready", "Cookie 已就绪"],
    ["needs_auth", "Cookie 需要重新授权"],
    ["unavailable", "115 当前不可用"],
  ] as const)("shows the %s validation state without replacing settings", async (status, message) => {
    const api = makeApi({
      validateP115Cookie: vi.fn().mockResolvedValue({ status, checked_at: "2026-07-25T02:00:00Z" }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    const p115Button = wrapper.findAll("button").find((button) => button.text().includes("115 推送"));
    await p115Button?.trigger("click");
    await wrapper.get("button.secondary-button").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain(message);
    expect(wrapper.text()).toContain("结构正常");
    expect(api.p115Settings).toHaveBeenCalledTimes(2);
  });
});
