import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import OrganizationView from "../src/views/OrganizationView.vue";

const organization = {
  schedule_enabled: true,
  auto_execute_enabled: false,
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

const emptyResult = {
  status: "unknown" as const,
  available_statuses: ["unknown", "success", "skipped", "deleted", "replace", "failed"],
  source_count: 0,
  scanned_count: 0,
  plan_count: 0,
  queued_count: 0,
  blocked_count: 0,
  blocked_details: [],
  items: [],
  finished_at: null,
  run_id: null,
};

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    organizationSettings: vi.fn().mockResolvedValue(organization),
    updateOrganizationSettings: vi.fn().mockResolvedValue({ ...organization, auto_execute_enabled: true, revision: 5 }),
    organizationResult: vi.fn().mockResolvedValue(emptyResult),
    runOrganizationNow: vi.fn().mockResolvedValue({ action: "run_now", queued: true, schedule_enabled: false, message_zh: "整理已排队", run_id: "org-run" }),
    stopOrganization: vi.fn().mockResolvedValue({ action: "stop", queued: false, schedule_enabled: false, message_zh: "已停止整理", run_id: null }),
    organizationPlans: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    organizationHistory: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    ...overrides,
  } as unknown as ApiClient;
}

async function openOrganizationTab(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(label));
  await button?.trigger("click");
  await flushPromises();
}

describe("OrganizationView", () => {
  it("shows the auto-organize dashboard on the status tab", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    expect(wrapper.get(".organization-auto-card").text()).toContain("已关闭");
    expect(wrapper.get(".organization-summary-list").text()).toContain("定时整理");
    expect(wrapper.get(".organization-summary-list").text()).toContain("30 分钟");
    expect(wrapper.findAll(".settings-note")).toHaveLength(1);
    expect(wrapper.get(".organization-result-state").text()).toContain("尚未整理");
    expect(wrapper.findAll("button").some((button) => button.text().includes("前往设置配置整理规则"))).toBe(true);
  });

  it("toggles auto-execute through updateOrganizationSettings", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("开启自动整理"))?.trigger("click");
    await flushPromises();

    expect(api.updateOrganizationSettings).toHaveBeenCalledWith({ auto_execute_enabled: true, revision: 4 });
    expect(wrapper.get(".organization-auto-card").text()).toContain("已开启");
  });

  it("runs organization now and surfaces the result", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("开始整理"))?.trigger("click");
    await flushPromises();

    expect(api.runOrganizationNow).toHaveBeenCalledTimes(1);
    expect(wrapper.get(".settings-action-message").text()).toContain("整理已排队");
  });

  it("shows scheduler-unavailable errors when run organization fails", async () => {
    const api = makeApi({
      runOrganizationNow: vi.fn().mockRejectedValue(
        new ApiError(
          "当前环境没有启动自动整理调度服务，整理任务尚未执行。",
          503,
          "organization_schedule_unavailable",
          { suggestion: "请在部署配置中启用整理计划服务后再试。", retryable: true, action: "inspect_configuration" },
        ),
      ),
    });
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("开始整理"))?.trigger("click");
    await flushPromises();

    expect(wrapper.get(".settings-action-message").text()).toContain("当前环境没有启动自动整理调度服务");
    expect(wrapper.get(".settings-action-message").text()).toContain("启用整理计划服务");
  });

  it("gives the review tab a heading above the workbench status filter", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await openOrganizationTab(wrapper, "待处理");

    expect(wrapper.get("h2").text()).toContain("待处理计划");
    for (const label of ["待确认", "已确认", "已忽略", "已失效"]) {
      expect(wrapper.findAll("button").some((button) => button.text().includes(label))).toBe(true);
    }
  });

  it("gives the history tab a heading", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await openOrganizationTab(wrapper, "历史");

    expect(wrapper.get("h2").text()).toContain("整理历史");
  });

  it("emits open-settings from the status tab", async () => {
    const api = makeApi();
    const wrapper = mount(OrganizationView, { props: { api } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("前往设置配置整理规则"))?.trigger("click");
    expect(wrapper.emitted("open-settings")).toHaveLength(1);
  });
});
