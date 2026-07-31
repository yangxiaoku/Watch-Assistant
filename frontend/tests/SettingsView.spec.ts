import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import { p115DeviceOptions } from "../src/p115DeviceTypes";
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
const inspection = { auto_start_enabled: true, revision: 0 };
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
const credentials = {
  revision: 0,
  tmdb: { configured: false, source: "environment" as const, last_updated_at: null },
  p115_cookie: { configured: true, source: "tgtodrive" as const, last_updated_at: "2026-07-25T02:00:00Z", structure_valid: true, ready: true },
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
const organization = {
  schedule_enabled: true,
  scan_interval_minutes: 30,
  source_directory_ids: ["1000"],
  target_directory_id: "2000",
  push_directory_id: "3500",
  video_extensions: ["mkv", "mp4"],
  metadata_extensions: ["srt", "nfo"],
  rename_enabled: true,
  media_probe_enabled: true,
  ai_identification_enabled: false,
  small_file_threshold_mb: 10,
  cleanup_empty_directories: false,
  strm_linkage_enabled: true,
  operation_delay_seconds: 1.5,
  include_children_category: false,
  include_concert_category: false,
  region_grouping_enabled: true,
  year_grouping_enabled: false,
  prefer_remux: true,
  prefer_resolution: true,
  prefer_dolby: false,
  conflict_mode: 2 as const,
  multi_version_enabled: false,
  revision: 4,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    settingsOverview: vi.fn().mockResolvedValue(overview),
    loggingSettings: vi.fn().mockResolvedValue(logging),
    inspectionSettings: vi.fn().mockResolvedValue(inspection),
    updateInspectionSettings: vi.fn().mockResolvedValue({ ...inspection, revision: 1 }),
    updateLoggingSettings: vi.fn().mockResolvedValue({ ...logging, revision: 8 }),
    p115Settings: vi.fn().mockResolvedValue(p115),
    credentialSettings: vi.fn().mockResolvedValue(credentials),
    updateTmdbCredential: vi.fn().mockResolvedValue({ ...credentials, revision: 1, tmdb: { configured: true, source: "managed", last_updated_at: "2026-07-25T03:00:00Z" } }),
    resetTmdbCredential: vi.fn().mockResolvedValue(credentials),
    updateP115Cookie: vi.fn().mockResolvedValue({ ...credentials, revision: 1, p115_cookie: { ...credentials.p115_cookie, source: "managed" } }),
    resetP115Cookie: vi.fn().mockResolvedValue(credentials),
    validateP115Cookie: vi.fn().mockResolvedValue({ status: "ready", checked_at: "2026-07-25T02:00:00Z" }),
    logs: vi.fn().mockResolvedValue(firstLogs),
    contentPolicy: vi.fn().mockResolvedValue(contentPolicy),
    updateContentPolicy: vi.fn().mockResolvedValue({ ...contentPolicy, revision: 4 }),
    organizationSettings: vi.fn().mockResolvedValue(organization),
    updateOrganizationSettings: vi.fn().mockResolvedValue({ ...organization, revision: 5 }),
    p115Directories: vi.fn().mockImplementation(async (directoryId?: string) => ({
      root_id: "0",
      parent_id: directoryId || "0",
      items: directoryId ? [] : [{ id: "3000", name: "推送目录" }],
      has_more: false,
      next_page: null,
    })),
    runOrganizationNow: vi.fn().mockResolvedValue({ action: "run_now", queued: true, schedule_enabled: false, message_zh: "整理已排队" }),
    stopOrganization: vi.fn().mockResolvedValue({ action: "stop", queued: false, schedule_enabled: false, message_zh: "已停止整理" }),
    ...overrides,
  } as unknown as ApiClient;
}

async function openLogs(wrapper: ReturnType<typeof mount>) {
  const button = wrapper.findAll("button").find((item) => item.text().includes("日志"));
  await button?.trigger("click");
  await flushPromises();
}

describe("SettingsView", () => {
  it("keeps all verified 115 QR device types available", () => {
    expect(p115DeviceOptions).toHaveLength(18);
    expect(p115DeviceOptions.map((option) => option.value)).toEqual([
      "android", "115android", "web", "ios", "115ios", "ipad", "115ipad", "os_linux",
      "os_windows", "os_mac", "tv", "apple_tv", "qandroid", "qios", "qipad", "alipaymini",
      "wechatmini", "harmony",
    ]);
    expect(p115DeviceOptions.map((option) => option.label)).toContain("115生活(支付宝小程序)");
    expect(p115DeviceOptions.map((option) => option.label)).toContain("115(鸿蒙端)");
  });

  it("shows the QR device picker before generating a QR code", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();

    const picker = wrapper.get('select[aria-label="115 设备类型"]');
    expect(picker.findAll("option")).toHaveLength(18);
    expect(picker.element.value).toBe("web");
  });

  it("keeps manual organization independent from the schedule switch", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    expect(wrapper.findAll("details.settings-subsection")).toHaveLength(6);
    const scheduleToggle = wrapper.get(".settings-subsection input[type=checkbox]");
    await scheduleToggle.setValue(false);
    await wrapper.get(".settings-subsection input[type=number]").setValue("10");
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(api.updateOrganizationSettings).toHaveBeenCalledWith(expect.objectContaining({
      schedule_enabled: false,
      scan_interval_minutes: 10,
      push_directory_id: "3500",
      revision: 4,
    }));
    await wrapper.findAll("button").find((button) => button.text().includes("立即整理"))?.trigger("click");
    expect(api.runOrganizationNow).toHaveBeenCalledTimes(1);
    await wrapper.findAll("button").find((button) => button.text().includes("停止整理"))?.trigger("click");
    expect(api.stopOrganization).toHaveBeenCalledTimes(1);
  });

  it("selects and persists a separate push directory from the managed scope", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("选择推送目录"))?.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("选择资源推送目录");
    await wrapper.findAll(".directory-picker-item")[0].trigger("click");
    await flushPromises();
    await wrapper.find(".directory-picker-toolbar .primary-button").trigger("click");
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(api.updateOrganizationSettings).toHaveBeenCalledWith(expect.objectContaining({
      push_directory_id: "3000",
      revision: 4,
    }));
  });

  it("opens the directory picker at the real 115 root", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("选择推送目录"))?.trigger("click");
    await flushPromises();

    expect(api.p115Directories).toHaveBeenCalledWith(undefined);
    expect(wrapper.text()).toContain("115 网盘根目录");
    expect(wrapper.text()).toContain("115 网盘");
    expect(wrapper.text()).not.toContain("当前配置的 115 受管目录");
    expect(wrapper.get(".directory-picker-toolbar .primary-button").text()).toContain("根目录不可直接选择");
    expect(wrapper.get(".directory-picker-toolbar .primary-button").attributes("disabled")).toBeDefined();
  });

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
    expect(wrapper.text()).toContain("页面不会回显已保存的 Cookie 原文");
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

  it("passes structured log filters to the API", async () => {
    const logsMock = vi.fn().mockResolvedValue({ ...firstLogs, next_cursor: null });
    const wrapper = mount(SettingsView, { props: { api: makeApi({ logs: logsMock }) } });
    await flushPromises();
    await openLogs(wrapper);

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

  it("loads and saves the persistent inspection auto-start setting", async () => {
    const api = makeApi({
      inspectionSettings: vi.fn().mockResolvedValue({ auto_start_enabled: true, revision: 3 }),
      updateInspectionSettings: vi.fn().mockResolvedValue({ auto_start_enabled: false, revision: 4 }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("资源检测"))?.trigger("click");
    const toggle = wrapper.get(".settings-toggle input");
    await toggle.setValue(false);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(api.updateInspectionSettings).toHaveBeenCalledWith({ auto_start_enabled: false, revision: 3 });
    expect(wrapper.text()).toContain("资源检测");
  });

  it("keeps the shared revision synchronized between logging and inspection saves", async () => {
    const inspectionCalls: unknown[] = [];
    const loggingCalls: unknown[] = [];
    const api = makeApi({
      inspectionSettings: vi.fn().mockResolvedValue({ auto_start_enabled: true, revision: 0 }),
      loggingSettings: vi.fn().mockResolvedValue({ ...logging, revision: 0 }),
      updateInspectionSettings: vi.fn().mockImplementation(async (value) => {
        inspectionCalls.push(value);
        return { auto_start_enabled: value.auto_start_enabled, revision: value.revision + 1 };
      }),
      updateLoggingSettings: vi.fn().mockImplementation(async (value) => {
        loggingCalls.push(value);
        return { ...logging, level: value.level, revision: value.revision + 1 };
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("资源检测"))?.trigger("click");
    await wrapper.get(".settings-toggle input").setValue(false);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("日志"))?.trigger("click");
    await wrapper.get(".logging-settings select").setValue("WARNING");
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("资源检测"))?.trigger("click");
    await wrapper.get(".settings-toggle input").setValue(true);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(inspectionCalls).toEqual([
      { auto_start_enabled: false, revision: 0 },
      { auto_start_enabled: true, revision: 2 },
    ]);
    expect(loggingCalls).toEqual([
      expect.objectContaining({ revision: 1, level: "WARNING" }),
    ]);
  });

  it("keeps the content draft after a failed save without advancing committed revision", async () => {
    const updateContentPolicy = vi.fn()
      .mockRejectedValueOnce(new ApiError("failed", 500))
      .mockResolvedValueOnce({ ...contentPolicy, hide_adult_media: false, revision: 4 });
    const api = makeApi({ updateContentPolicy });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("内容安全"))?.trigger("click");
    await flushPromises();

    await wrapper.get(".settings-policy-row input").setValue(false);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.get(".settings-policy-row input").element).toHaveProperty("checked", false);
    expect(wrapper.text()).toContain("当前版本 3");

    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();
    expect(updateContentPolicy).toHaveBeenNthCalledWith(2, expect.objectContaining({ revision: 3, hide_adult_media: false }));
    expect(wrapper.text()).toContain("当前版本 4");
  });

  it("starts and stops the log timer with section, visibility, and toggle state", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const logsMock = vi.fn().mockResolvedValue(firstLogs);
    const wrapper = mount(SettingsView, { props: { api: makeApi({ logs: logsMock }) } });
    await flushPromises();
    await openLogs(wrapper);
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

    const overviewButton = wrapper.findAll("button").find((button) => button.text().includes("概览"));
    await overviewButton?.trigger("click");
    vi.advanceTimersByTime(10000);
    await flushPromises();
    expect(logsMock).toHaveBeenCalledTimes(callsAfterLoad + 1);
    wrapper.unmount();
    vi.useRealTimers();
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

  it("saves both managed credentials with the shared revision and clears drafts", async () => {
    const updatedTmdb = { ...credentials, revision: 1, tmdb: { configured: true, source: "managed" as const, last_updated_at: "2026-07-25T03:00:00Z" } };
    const updatedP115 = { ...updatedTmdb, revision: 2, p115_cookie: { ...credentials.p115_cookie, source: "managed" as const } };
    const api = makeApi({
      updateTmdbCredential: vi.fn().mockResolvedValue(updatedTmdb),
      updateP115Cookie: vi.fn().mockResolvedValue(updatedP115),
      p115Settings: vi.fn().mockResolvedValue({ ...p115, cookie: { ...p115.cookie, source: "managed" as const } }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panels = wrapper.findAll(".credential-panel");
    const tmdbInput = panels[0].get("input[type=password]");
    await tmdbInput.setValue("test-tmdb-key-123");
    await panels[0].get(".primary-button").trigger("click");
    await flushPromises();
    expect(api.updateTmdbCredential).toHaveBeenCalledWith("test-tmdb-key-123", 0);
    expect((tmdbInput.element as HTMLInputElement).value).toBe("");
    expect(wrapper.html()).not.toContain("test-tmdb-key-123");

    const p115Input = panels[1].get("input[type=password]");
    await p115Input.setValue("UID=test-cookie; CID=fake");
    await panels[1].get(".primary-button").trigger("click");
    await flushPromises();
    expect(api.updateP115Cookie).toHaveBeenCalledWith("UID=test-cookie; CID=fake", 1);
    expect((p115Input.element as HTMLInputElement).value).toBe("");

    await panels[0].get(".secondary-button").trigger("click");
    await flushPromises();
    expect(api.resetTmdbCredential).toHaveBeenCalledWith(2);
  });

  it.each([
    [409, "设置已被其他请求修改"],
    [422, "凭据格式或验证未通过"],
    [429, "操作过于频繁"],
    [503, "凭据服务暂不可用"],
  ] as const)("maps credential status %s without losing the draft", async (status, message) => {
    const api = makeApi({ updateTmdbCredential: vi.fn().mockRejectedValue(new ApiError("secret backend detail", status)) });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panel = wrapper.findAll(".credential-panel")[0];
    const input = panel.get("input[type=password]");
    await input.setValue("draft-only-secret");
    await panel.get(".primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain(message);
    expect((input.element as HTMLInputElement).value).toBe("draft-only-secret");
    expect(wrapper.text()).not.toContain("secret backend detail");
  });

  it("gates repeated credential submissions while the first request is pending", async () => {
    let resolveSave: ((value: typeof credentials) => void) | undefined;
    const updateTmdbCredential = vi.fn().mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    const api = makeApi({ updateTmdbCredential });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panel = wrapper.findAll(".credential-panel")[0];
    await panel.get("input[type=password]").setValue("duplicate-click-secret");
    await panel.get(".primary-button").trigger("click");
    await panel.get(".primary-button").trigger("click");
    expect(updateTmdbCredential).toHaveBeenCalledTimes(1);
    resolveSave?.({ ...credentials, revision: 1 });
    await flushPromises();
  });

  it("shares one mutation gate across both credential panels and disables both drafts", async () => {
    let resolveSave: ((value: typeof credentials) => void) | undefined;
    const api = makeApi({
      credentialSettings: vi.fn().mockResolvedValue({ ...credentials, p115_cookie: { ...credentials.p115_cookie, source: "managed" as const } }),
      updateTmdbCredential: vi.fn().mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; })),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panels = wrapper.findAll(".credential-panel");
    await panels[0].get("input[type=password]").setValue("tmdb-pending-secret");
    await panels[1].get("input[type=password]").setValue("p115-pending-secret");
    await panels[0].get(".primary-button").trigger("click");
    expect((panels[0].get("input[type=password]").element as HTMLInputElement).disabled).toBe(true);
    expect((panels[1].get("input[type=password]").element as HTMLInputElement).disabled).toBe(true);
    await panels[1].get(".primary-button").trigger("click");
    await panels[1].findAll(".secondary-button")[0].trigger("click");
    expect(api.updateP115Cookie).not.toHaveBeenCalled();
    expect(api.resetP115Cookie).not.toHaveBeenCalled();
    resolveSave?.({ ...credentials, revision: 1 });
    await flushPromises();
  });

  it("blocks P115 mutations while current Cookie validation is pending", async () => {
    let resolveValidation: ((value: { status: "ready"; checked_at: string }) => void) | undefined;
    const api = makeApi({
      credentialSettings: vi.fn().mockResolvedValue({ ...credentials, p115_cookie: { ...credentials.p115_cookie, source: "managed" as const } }),
      validateP115Cookie: vi.fn().mockImplementation(() => new Promise((resolve) => { resolveValidation = resolve; })),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panel = wrapper.findAll(".credential-panel")[1];
    await panel.get(".credential-actions .secondary-button:last-child").trigger("click");
    expect((panel.get(".credential-actions .primary-button").element as HTMLButtonElement).disabled).toBe(true);
    expect((panel.findAll(".credential-actions .secondary-button")[0].element as HTMLButtonElement).disabled).toBe(true);
    await panel.get(".credential-actions .primary-button").trigger("click");
    await panel.findAll(".credential-actions .secondary-button")[0].trigger("click");
    expect(api.updateP115Cookie).not.toHaveBeenCalled();
    expect(api.resetP115Cookie).not.toHaveBeenCalled();
    resolveValidation?.({ status: "ready", checked_at: "2026-07-25T02:00:00Z" });
    await flushPromises();
  });

  it("does not reset environment or fallback credentials, including direct handler calls", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panels = wrapper.findAll(".credential-panel");
    expect((panels[0].findAll(".secondary-button")[0].element as HTMLButtonElement).disabled).toBe(true);
    expect((panels[1].findAll(".secondary-button")[0].element as HTMLButtonElement).disabled).toBe(true);
    await panels[0].findAll(".secondary-button")[0].trigger("click");
    await panels[1].findAll(".secondary-button")[0].trigger("click");
    expect(api.resetTmdbCredential).not.toHaveBeenCalled();
    expect(api.resetP115Cookie).not.toHaveBeenCalled();
  });

  it("does not update credential state after an in-flight mutation is unmounted", async () => {
    let resolveSave: ((value: typeof credentials) => void) | undefined;
    const api = makeApi({ updateTmdbCredential: vi.fn().mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; })) });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panel = wrapper.findAll(".credential-panel")[0];
    await panel.get("input[type=password]").setValue("unmounted-secret");
    await panel.get(".primary-button").trigger("click");
    wrapper.unmount();
    resolveSave?.({ ...credentials, revision: 1 });
    await flushPromises();
    expect(api.updateTmdbCredential).toHaveBeenCalledTimes(1);
  });
});
