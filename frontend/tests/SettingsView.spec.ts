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
    source: "file" as const,
    configured: true,
    structure_valid: true,
  },
  target_configured: true,
  max_concurrency: 1,
};
const credentials = {
  revision: 0,
  tmdb: { configured: false, source: "environment" as const, last_updated_at: null },
  p115_cookie: { configured: true, source: "file" as const, last_updated_at: "2026-07-25T02:00:00Z", structure_valid: true, ready: true },
};

const firstLogs: LogsResponse = {
  items: [
    { id: 1, timestamp: "2026-07-25T02:00:00Z", level: "INFO" as const, category: "system" as const, message: "service.started", message_zh: "服务已启动" },
    { id: 2, timestamp: "2026-07-25T02:01:00Z", level: "WARNING" as const, category: "cache" as const, message: "cache.refreshed", message_zh: "缓存已刷新" },
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
  source_directory_labels: ["待整理/来源"],
  target_directory_id: "2000",
  target_directory_label: "媒体库/归档",
  push_directory_id: "3500",
  push_directory_label: "媒体库/推送",
  video_extensions: ["mkv", "mp4"],
  metadata_extensions: ["srt", "nfo"],
  rename_enabled: true,
  media_probe_enabled: true,
  ai_identification_enabled: false,
  small_file_threshold_mb: 10,
  cleanup_empty_directories: false,
  auto_cleanup_junk_files: false,
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
const prowlarrSettings = {
  source: "none" as const,
  enabled: false,
  configured: false,
  base_url: null,
  api_key_configured: false,
  api_key_source: "none" as const,
  last_updated_at: null,
  revision: 0,
};
const p115CheckinSettings = { enabled: true, check_in_time: "00:05" };
const p115CheckinStatus = {
  enabled: true,
  is_sign_today: true,
  continuous_day: 3,
  points_num: "150",
  error_code: null,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    settingsOverview: vi.fn().mockResolvedValue(overview),
    loggingSettings: vi.fn().mockResolvedValue(logging),
    inspectionSettings: vi.fn().mockResolvedValue(inspection),
    updateInspectionSettings: vi.fn().mockResolvedValue({ ...inspection, revision: 1 }),
    updateLoggingSettings: vi.fn().mockResolvedValue({ ...logging, revision: 8 }),
    p115Settings: vi.fn().mockResolvedValue(p115),
    prowlarrSettings: vi.fn().mockResolvedValue(prowlarrSettings),
    updateProwlarrSettings: vi.fn().mockResolvedValue({ ...prowlarrSettings, source: "managed", enabled: true, configured: true, base_url: "http://prowlarr:9696", api_key_configured: true, api_key_source: "managed", revision: 1 }),
    resetProwlarrSettings: vi.fn().mockResolvedValue(prowlarrSettings),
    verifyProwlarr: vi.fn().mockResolvedValue({ source: "prowlarr", status: "disabled", configured: false, base_url: null, message_code: "prowlarr_disabled", checked_at: "2026-08-01T10:01:00Z" }),
    credentialSettings: vi.fn().mockResolvedValue(credentials),
    updateTmdbCredential: vi.fn().mockResolvedValue({ ...credentials, revision: 1, tmdb: { configured: true, source: "managed", last_updated_at: "2026-07-25T03:00:00Z" } }),
    resetTmdbCredential: vi.fn().mockResolvedValue(credentials),
    updateP115Cookie: vi.fn().mockResolvedValue({ ...credentials, revision: 1, p115_cookie: { ...credentials.p115_cookie, source: "managed" } }),
    resetP115Cookie: vi.fn().mockResolvedValue(credentials),
    validateP115Cookie: vi.fn().mockResolvedValue({ status: "ready", checked_at: "2026-07-25T02:00:00Z" }),
    p115CheckinSettings: vi.fn().mockResolvedValue(p115CheckinSettings),
    updateP115CheckinSettings: vi.fn().mockResolvedValue(p115CheckinSettings),
    p115CheckinStatus: vi.fn().mockResolvedValue(p115CheckinStatus),
    logs: vi.fn().mockResolvedValue(firstLogs),
    contentPolicy: vi.fn().mockResolvedValue(contentPolicy),
    updateContentPolicy: vi.fn().mockResolvedValue({ ...contentPolicy, revision: 4 }),
    organizationSettings: vi.fn().mockResolvedValue(organization),
    organizationResult: vi.fn().mockResolvedValue({
      status: "unknown",
      available_statuses: ["unknown", "success", "skipped", "deleted", "replace", "failed"],
      source_count: 0,
      scanned_count: 0,
      plan_count: 0,
      queued_count: 0,
      blocked_count: 0,
      blocked_details: [],
      finished_at: null,
      run_id: null,
    }),
    updateOrganizationSettings: vi.fn().mockResolvedValue({ ...organization, revision: 5 }),
    p115Directories: vi.fn().mockImplementation(async (directoryId?: string) => ({
      root_id: "0",
      parent_id: directoryId || "0",
      items: directoryId ? [] : [{ id: "3000", name: "推送目录" }],
      has_more: false,
      next_page: null,
    })),
    runOrganizationNow: vi.fn().mockResolvedValue({ action: "run_now", queued: true, schedule_enabled: false, message_zh: "整理已排队", run_id: "org-test-run" }),
    stopOrganization: vi.fn().mockResolvedValue({ action: "stop", queued: false, schedule_enabled: false, message_zh: "已停止整理", run_id: null }),
    ...overrides,
  } as unknown as ApiClient;
}

async function openLogs(wrapper: ReturnType<typeof mount>) {
  const button = wrapper.findAll("button").find((item) => item.text().includes("日志"));
  await button?.trigger("click");
  await flushPromises();
}

describe("SettingsView", () => {
  it("does not repeat the disabled Prowlarr status", async () => {
    const wrapper = mount(SettingsView, { props: { api: makeApi(), initialSection: "prowlarr" } });
    await flushPromises();

    const statusText = wrapper.get(".prowlarr-status-line").text();
    expect(statusText.match(/未启用/g)?.length).toBe(1);
  });

  it("shows an enabled but unverified Prowlarr source as unverified", async () => {
    const api = makeApi({
      prowlarrSettings: vi.fn().mockResolvedValue({
        ...prowlarrSettings,
        source: "managed",
        enabled: true,
        configured: true,
        base_url: "http://prowlarr:9696",
        api_key_configured: true,
        api_key_source: "managed",
        health_state: "unverified",
        health_reason_zh: "Prowlarr 配置已保存，尚未完成只读连接验证。",
        health_retry_after_seconds: null,
      }),
    });
    const wrapper = mount(SettingsView, { props: { api, initialSection: "prowlarr" } });
    await flushPromises();

    expect(wrapper.get(".prowlarr-status-line").text()).toContain("配置未验证");
    expect(wrapper.get(".prowlarr-status-line").text()).not.toContain("已配置");
    expect(wrapper.get(".prowlarr-status-line > span:last-child").classes()).toContain("status-degraded");
    expect(wrapper.get(".prowlarr-health-note").text()).toContain("尚未完成只读连接验证");
  });

  it("shows safe Prowlarr backoff reason and retry time without exposing the API Key", async () => {
    const api = makeApi({
      prowlarrSettings: vi.fn().mockResolvedValue({
        ...prowlarrSettings,
        source: "managed",
        enabled: true,
        configured: true,
        base_url: "http://prowlarr:9696",
        api_key_configured: true,
        api_key_source: "managed",
        health_state: "backoff",
        health_reason_zh: "Prowlarr 当前受到限流，系统将在稍后重试。",
        health_retry_after_seconds: 42,
      }),
    });
    const wrapper = mount(SettingsView, { props: { api, initialSection: "prowlarr" } });
    await flushPromises();

    expect(wrapper.get(".prowlarr-status-line").text()).toContain("暂时退避");
    expect(wrapper.get(".prowlarr-health-note").text()).toContain("42 秒后重试");
    expect(wrapper.get(".prowlarr-health-note").text()).toContain("受到限流");
    expect(wrapper.html()).not.toContain("fixture-api-key");
  });

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

  it("keeps P115 device loading errors visible instead of showing a false empty state", async () => {
    const p115Devices = vi.fn()
      .mockRejectedValueOnce(new ApiError("扫码设备服务暂不可用，请稍后重试", 503, "p115_settings_unavailable"))
      .mockResolvedValueOnce({ items: [] });
    const api = makeApi({ p115Devices });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();

    expect(wrapper.get(".p115-device-list .inline-alert").text()).toContain("扫码设备服务暂不可用，请稍后重试");
    expect(wrapper.get(".p115-device-list").text()).not.toContain("暂无扫码设备");
    await wrapper.get(".p115-device-list .inline-alert .text-button").trigger("click");
    await flushPromises();

    expect(p115Devices).toHaveBeenCalledTimes(2);
    expect(wrapper.get(".p115-device-list").text()).toContain("暂无扫码设备");
    expect(wrapper.find(".p115-device-list .inline-alert").exists()).toBe(false);
  });

  it("keeps organization settings independent from the schedule switch and deduplicated from the workbench", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    expect(wrapper.findAll("details.settings-subsection")).toHaveLength(3);
    expect(wrapper.findAll("button").some((button) => button.text().includes("开始整理"))).toBe(false);
    expect(wrapper.find(".organization-result-panel").exists()).toBe(false);
    expect(wrapper.findAll("button").some((button) => button.text().includes("前往整理页"))).toBe(true);
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
  });

  it("persists the junk-file auto cleanup toggle through the organization save", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    // 展开「高级设置」折叠区(自动清理开关位于其中)
    const advancedSummary = wrapper.findAll("details.settings-subsection summary").find((summary) => summary.text().includes("高级设置"));
    await advancedSummary?.trigger("click");
    await flushPromises();

    const junkToggle = wrapper
      .findAll("label.settings-toggle")
      .find((label) => label.text().includes("自动清理广告垃圾文件"))
      ?.find("input[type=checkbox]");
    expect(junkToggle).toBeDefined();
    expect((junkToggle!.element as HTMLInputElement).checked).toBe(false); // 默认关闭
    await junkToggle!.setValue(true);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(api.updateOrganizationSettings).toHaveBeenCalledWith(expect.objectContaining({
      auto_cleanup_junk_files: true,
      revision: 4,
    }));
  });

  it("preserves the organization draft and reloads latest settings on a version conflict", async () => {
    const latest = { ...organization, revision: 9 };
    const api = makeApi({
      updateOrganizationSettings: vi.fn().mockRejectedValue(new ApiError("设置已在其他位置更新", 409, "settings_conflict")),
      organizationSettings: vi.fn()
        .mockResolvedValueOnce(organization)
        .mockResolvedValueOnce(latest),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();

    const scheduleToggle = wrapper.get(".settings-subsection input[type=checkbox]");
    await scheduleToggle.setValue(false);
    await wrapper.get(".settings-save-bar .primary-button").trigger("click");
    await flushPromises();

    expect(api.organizationSettings).toHaveBeenCalledTimes(2);
    expect(wrapper.get(".inline-alert-error").text()).toContain("已加载最新版本");
    expect((scheduleToggle.element as HTMLInputElement).checked).toBe(false);
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
      push_directory_label: "推送目录",
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

  it("renders the real overview fields and p115 connection status inside credentials", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("2026.07.25");
    expect(wrapper.text()).toContain("1 天 1 小时");
    expect(wrapper.text()).toContain("3.0 MB");
    expect(wrapper.text()).toContain("内容检测");
    expect(wrapper.text()).toContain("115 分享转存");
    expect(wrapper.text()).toContain("未配置");
    expect(wrapper.text()).toContain("只读可用");
    expect(wrapper.text()).toContain("可执行");
    expect(wrapper.text()).not.toContain("组件状态");
    expect(wrapper.text()).not.toContain("配置版本");
    expect(wrapper.find("input[type=password]").exists()).toBe(false);

    const credentialsButton = wrapper.findAll("button").find((button) => button.text().includes("连接配置"));
    await credentialsButton?.trigger("click");
    expect(wrapper.text()).toContain("来源");
    expect(wrapper.text()).toContain("文件");
    expect(wrapper.text()).toContain("结构正常");
    expect(wrapper.text()).toContain("已就绪");
    expect(wrapper.text()).toContain("页面不会回显已保存的 Cookie 原文");
  });

  it("maps capability readiness to actionable Chinese states", async () => {
    const api = makeApi({
      settingsOverview: vi.fn().mockResolvedValue({
        ...overview,
        capabilities: {
          inspection: true,
          magnet: false,
          share: false,
          organization_plan: false,
          organization_execution: true,
          organization_write: false,
          organization_empty_directory_cleanup: false,
          permanent_delete: false,
          strm_full: false,
          strm_incremental: false,
          strm_cleanup: false,
          strm_playback: false,
        },
        capability_statuses: {
          inspection: { state: "runtime_healthy", state_zh: "运行健康", last_success_at: null },
          magnet: { state: "unconfigured", state_zh: "未配置", last_success_at: null },
          organization_execution: { state: "runtime_healthy", state_zh: "运行健康", last_success_at: null },
          organization_write: { state: "configured", state_zh: "已配置", last_success_at: null },
          strm_full: { state: "configured", state_zh: "已配置", last_success_at: null },
        },
        capability_details: {
          strm_full: { enabled: false, reason_code: "strm_full_disabled", reason_zh: "STRM 全量生成未启用，请检查部署功能开关。", settings_section: "overview" },
        },
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("只读可用");
    expect(wrapper.text()).toContain("未配置");
    expect(wrapper.text()).toContain("已配置但关闭");
    expect(wrapper.text()).toContain("契约未验证");
    expect(wrapper.text()).toContain("可执行");
    expect(wrapper.text()).toContain("下一步：STRM 全量生成未启用，请检查部署功能开关。");
  });

  it("shortens release and keeps directory IDs and error codes out of the main prompt", async () => {
    const fullRelease = "0123456789abcdef0123456789abcdef01234567";
    const fullCid = "9876543210987654321";
    const errorCode = "scan_incomplete";
    const api = makeApi({
      settingsOverview: vi.fn().mockResolvedValue({ ...overview, release: fullRelease }),
      organizationSettings: vi.fn().mockResolvedValue({
        ...organization,
        source_directory_ids: [fullCid],
        source_directory_labels: ["待整理/来源"],
        target_directory_id: fullCid,
        target_directory_label: "媒体库/归档",
        push_directory_id: fullCid,
        push_directory_label: "媒体库/推送",
      }),
      organizationResult: vi.fn().mockResolvedValue({
        status: "failed",
        available_statuses: ["unknown", "success", "skipped", "deleted", "replace", "failed"],
        source_count: 1,
        scanned_count: 0,
        plan_count: 0,
        queued_count: 0,
        blocked_count: 1,
        blocked_details: [{ source_directory_id: fullCid, phase: "scan", error_code: errorCode, message_zh: "扫描未完成。", next_step_zh: "重新扫描。" }],
        items: [],
        finished_at: null,
        run_id: null,
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain(fullRelease.slice(0, 7));
    expect(wrapper.text()).not.toContain(fullRelease);
    await wrapper.findAll("button").find((button) => button.text().includes("115 整理"))?.trigger("click");
    await flushPromises();
    expect(wrapper.text()).not.toContain(fullCid);
    expect(wrapper.text()).not.toContain(errorCode);
    const diagnostics = wrapper.get(".directory-diagnostic");
    expect(diagnostics.element.hasAttribute("open")).toBe(false);
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

    await wrapper.get(".inline-alert-action").trigger("click");
    await flushPromises();
    expect(wrapper.find(".inline-alert-error").exists()).toBe(false);
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
    await wrapper.findAll("button").find((button) => button.text().includes("连接配置"))?.trigger("click");
    await flushPromises();
    const panel = wrapper.findAll(".credential-panel")[1];
    await panel.get(".credential-actions .secondary-button:last-child").trigger("click");
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

  it("saves Prowlarr settings without echoing the API Key and verifies through ApiClient", async () => {
    const api = makeApi({
      prowlarrSettings: vi.fn().mockResolvedValue({ ...prowlarrSettings, source: "managed", enabled: true, configured: true, base_url: "http://prowlarr:9696", api_key_configured: true, api_key_source: "managed", revision: 4 }),
      updateProwlarrSettings: vi.fn().mockResolvedValue({ ...prowlarrSettings, source: "managed", enabled: true, configured: true, base_url: "http://prowlarr:9696", api_key_configured: true, api_key_source: "managed", revision: 5 }),
      verifyProwlarr: vi.fn().mockResolvedValue({ source: "prowlarr", status: "available", configured: true, base_url: "http://prowlarr:9696", message_code: null, checked_at: "2026-08-01T10:01:00Z" }),
    });
    const wrapper = mount(SettingsView, { props: { api, initialSection: "prowlarr" } });
    await flushPromises();

    expect(wrapper.get("#prowlarr-title").text()).toBe("Prowlarr");
    expect(wrapper.text()).toContain("已配置");
    expect((wrapper.get("#prowlarr-api-key").element as HTMLInputElement).value).toBe("");
    await wrapper.get("#prowlarr-base-url").setValue("http://prowlarr:9696");
    await wrapper.get("#prowlarr-api-key").setValue("fixture-only");
    await wrapper.get("button.primary-button").trigger("click");
    await flushPromises();
    expect(api.updateProwlarrSettings).toHaveBeenCalledWith({ enabled: true, base_url: "http://prowlarr:9696", api_key: "fixture-only", revision: 4 });
    expect((wrapper.get("#prowlarr-api-key").element as HTMLInputElement).value).toBe("");

    const verifyButton = wrapper.findAll("button").find((button) => button.text().includes("验证 Prowlarr 连接"));
    await verifyButton?.trigger("click");
    await flushPromises();
    expect(api.verifyProwlarr).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("连接验证成功");
  });

  it("offers reload on a Prowlarr revision conflict", async () => {
    const prowlarrSettingsMock = vi.fn()
      .mockResolvedValueOnce({ ...prowlarrSettings, source: "managed", enabled: true, configured: true, base_url: "http://prowlarr:9696", api_key_configured: true, api_key_source: "managed", revision: 2 })
      .mockResolvedValueOnce({ ...prowlarrSettings, source: "managed", enabled: true, configured: true, base_url: "http://prowlarr:9696", api_key_configured: true, api_key_source: "managed", revision: 3 });
    const api = makeApi({
      prowlarrSettings: prowlarrSettingsMock,
      updateProwlarrSettings: vi.fn().mockRejectedValue(new ApiError("设置已被其他请求修改", 409, "settings_conflict")),
    });
    const wrapper = mount(SettingsView, { props: { api, initialSection: "prowlarr" } });
    await flushPromises();
    await wrapper.get("#prowlarr-base-url").setValue("http://changed:9696");
    await wrapper.get("button.primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("设置已被其他请求修改");
    await wrapper.get(".inline-alert-action").trigger("click");
    await flushPromises();
    expect(prowlarrSettingsMock).toHaveBeenCalledTimes(2);
  });

  it("saves the 115 check-in toggle through PATCH when switched", async () => {
    const api = makeApi({
      updateP115CheckinSettings: vi.fn().mockResolvedValue({ ...p115CheckinSettings, enabled: false }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    const toggle = wrapper.get(".checkin-toggle input");
    await toggle.setValue(false);
    await flushPromises();
    expect(api.updateP115CheckinSettings).toHaveBeenCalledWith({ enabled: false, check_in_time: "00:05" });
  });

  it("shows the check-in status as signed in with continuous days and points", async () => {
    const api = makeApi();
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("今日已签到");
    expect(wrapper.text()).toContain("连续签到");
    expect(wrapper.text()).toContain("3");
    expect(wrapper.text()).toContain("150");
  });

  it("shows the check-in status as not signed in", async () => {
    const api = makeApi({
      p115CheckinStatus: vi.fn().mockResolvedValue({
        enabled: true,
        is_sign_today: false,
        continuous_day: 0,
        points_num: "",
        error_code: null,
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("今日未签到");
  });

  it("shows 未启用 when the check-in feature is disabled", async () => {
    const api = makeApi({
      p115CheckinStatus: vi.fn().mockResolvedValue({
        enabled: false,
        is_sign_today: false,
        continuous_day: 0,
        points_num: "",
        error_code: null,
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("未启用");
  });

  it("maps a check-in status error code to a readable message", async () => {
    const api = makeApi({
      p115CheckinStatus: vi.fn().mockResolvedValue({
        enabled: true,
        is_sign_today: false,
        continuous_day: 0,
        points_num: "",
        error_code: "credential_unavailable",
      }),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("登录凭据不可用");
  });

  it("reverts the check-in toggle and shows an error when the PATCH fails", async () => {
    const api = makeApi({
      updateP115CheckinSettings: vi.fn().mockRejectedValue(new ApiError("签到设置保存失败", 500, "checkin_save_failed")),
    });
    const wrapper = mount(SettingsView, { props: { api } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("115 自动签到"))?.trigger("click");
    await flushPromises();

    const toggle = wrapper.get(".checkin-toggle input");
    await toggle.setValue(false);
    await flushPromises();
    expect(api.updateP115CheckinSettings).toHaveBeenCalledWith({ enabled: false, check_in_time: "00:05" });
    expect((toggle.element as HTMLInputElement).checked).toBe(true);
    expect(wrapper.text()).toContain("签到设置保存失败");
  });
});
