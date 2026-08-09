import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api";
import type { LogsResponse } from "../src/types";
import LogsView from "../src/views/LogsView.vue";

const firstLogs: LogsResponse = {
  items: [
    { id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO" as const, category: "system" as const, message: "service.started", message_zh: "服务已启动" },
    { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING" as const, category: "cache" as const, message: "cache.refreshed", message_zh: "缓存已刷新" },
  ],
  next_cursor: 20,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    logs: vi.fn().mockResolvedValue(firstLogs),
    ...overrides,
  } as unknown as ApiClient;
}

async function openAdvanced(wrapper: ReturnType<typeof mount>) {
  const button = wrapper.findAll("button").find((item) => item.text().includes("高级筛选"));
  await button?.trigger("click");
  await flushPromises();
}

describe("LogsView", () => {
  it("loads the first cursor page and appends later pages with id de-duplication", async () => {
    const api = makeApi({
      logs: vi.fn()
        .mockResolvedValueOnce(firstLogs)
        .mockResolvedValueOnce({
          items: [
            { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING" as const, category: "cache" as const, message: "cache.refreshed", message_zh: "缓存已刷新（重复）" },
            { id: 3, timestamp: "2026-07-25T02:02:00Z", level: "ERROR" as const, category: "security" as const, message: "auth.relogin_required", message_zh: "需要重新登录" },
          ],
          next_cursor: null,
        }),
    });
    const wrapper = mount(LogsView, { props: { api } });
    await flushPromises();

    expect(api.logs).toHaveBeenCalledWith({ limit: 20, cursor: undefined, category: undefined });
    expect(wrapper.find(".settings-pagination").text()).toContain("已加载 2 条");
    await wrapper.get(".settings-pagination button").trigger("click");
    await flushPromises();

    expect(api.logs).toHaveBeenLastCalledWith({ limit: 20, cursor: 20, category: undefined });
    expect(wrapper.findAll(".settings-log-item")).toHaveLength(3);
    expect(wrapper.text()).toContain("需要重新登录");
    expect(wrapper.find(".settings-pagination button").exists()).toBe(false);
  });

  it("passes structured log filters to the API", async () => {
    const logsMock = vi.fn().mockResolvedValue({ ...firstLogs, next_cursor: null });
    const wrapper = mount(LogsView, { props: { api: makeApi({ logs: logsMock }) } });
    await flushPromises();
    await openAdvanced(wrapper);

    const selects = wrapper.findAll(".settings-filter-row select");
    await selects[1].setValue("ERROR");
    await selects[2].setValue("failed");
    const inputs = wrapper.findAll(".settings-filter-row input");
    await inputs[0].setValue("task.failed");
    await inputs[0].trigger("keyup", { key: "Enter" });
    await flushPromises();

    expect(logsMock).toHaveBeenLastCalledWith({
      limit: 20,
      cursor: undefined,
      category: undefined,
      level: "ERROR",
      status: "failed",
      eventCode: "task.failed",
    });
  });

  it("invalidates an older category request before applying its response", async () => {
    let resolveFirst: ((value: LogsResponse) => void) | undefined;
    let resolveSecond: ((value: LogsResponse) => void) | undefined;
    const api = makeApi({
      logs: vi.fn()
        .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
        .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; })),
    });
    const wrapper = mount(LogsView, { props: { api } });
    await flushPromises();

    const category = wrapper.get(".settings-filter-row select");
    await category.setValue("search");
    resolveSecond?.({ ...firstLogs, items: [{ ...firstLogs.items[0], id: 9, category: "search", message: "search.new_category", message_zh: "新分类响应" }], next_cursor: null });
    await flushPromises();
    resolveFirst?.({ ...firstLogs, items: [{ ...firstLogs.items[0], id: 1, message: "search.old_category", message_zh: "旧分类响应" }], next_cursor: null });
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
          items: [{ id: 3, timestamp: "2026-07-25T02:02:00Z", level: "INFO" as const, category: "system" as const, message: "system.old_page", message_zh: "旧页" }],
          next_cursor: null,
        })
        .mockResolvedValueOnce({
          items: [{ id: 4, timestamp: "2026-07-25T02:03:00Z", level: "INFO" as const, category: "system" as const, message: "system.auto_refresh", message_zh: "自动刷新" }],
          next_cursor: 20,
        }),
    });
    const wrapper = mount(LogsView, { props: { api } });
    await flushPromises();
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

  it("pauses auto refresh while the page is hidden", async () => {
    vi.useFakeTimers();
    const logsMock = vi.fn().mockResolvedValue(firstLogs);
    const wrapper = mount(LogsView, { props: { api: makeApi({ logs: logsMock }) } });
    await flushPromises();
    const callsAfterLoad = logsMock.mock.calls.length;

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock.mock.calls.length).toBe(callsAfterLoad);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock.mock.calls.length).toBeGreaterThan(callsAfterLoad);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("starts and stops the log timer with the auto-refresh toggle", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const logsMock = vi.fn().mockResolvedValue(firstLogs);
    const wrapper = mount(LogsView, { props: { api: makeApi({ logs: logsMock }) } });
    await flushPromises();
    const callsAfterLoad = logsMock.mock.calls.length;

    const autoRefresh = wrapper.get(".settings-section-actions input[type=checkbox]");
    await autoRefresh.setValue(false);
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock).toHaveBeenCalledTimes(callsAfterLoad);

    await autoRefresh.setValue(true);
    vi.advanceTimersByTime(5000);
    await flushPromises();
    expect(logsMock).toHaveBeenCalledTimes(callsAfterLoad + 1);
    wrapper.unmount();
    vi.useRealTimers();
  });
});
