import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import NotificationCenterView from "../src/views/NotificationCenterView.vue";

const notifications = [
  {
    id: "notification-1",
    event_code: "workflow.completed",
    severity: "info" as const,
    title_zh: "任务已完成",
    message_zh: "《测试电影》的任务已完成。",
    action_type: "workflow",
    action_id: "workflow-1",
    aggregate_count: 2,
    read_at: null,
    created_at: "2026-07-29T01:00:00Z",
    updated_at: "2026-07-29T01:01:00Z",
  },
  {
    id: "notification-2",
    event_code: "credential.expired",
    severity: "security" as const,
    title_zh: "连接需要重新授权",
    message_zh: "115 连接已失效，请检查设置。",
    action_type: "settings",
    action_id: null,
    aggregate_count: 1,
    read_at: "2026-07-29T01:02:00Z",
    created_at: "2026-07-29T01:02:00Z",
    updated_at: "2026-07-29T01:02:00Z",
  },
  {
    id: "notification-3",
    event_code: "organize.needs_review",
    severity: "warning" as const,
    title_zh: "整理预览需要人工确认",
    message_zh: "整理预览涉及 1 个文件，需要人工确认后继续",
    action_type: "organization_plan",
    action_id: "plan-1",
    aggregate_count: 1,
    read_at: null,
    created_at: "2026-07-29T01:03:00Z",
    updated_at: "2026-07-29T01:03:00Z",
  },
  {
    id: "notification-4",
    event_code: "subscription.auto_paused",
    severity: "info" as const,
    title_zh: "订阅已自动暂停",
    message_zh: "tv订阅整季资源已齐全，已自动暂停",
    action_type: "subscription",
    action_id: "sub_auto_paused",
    aggregate_count: 1,
    read_at: null,
    created_at: "2026-07-29T01:04:00Z",
    updated_at: "2026-07-29T01:04:00Z",
  },
];

function makeApi() {
  return {
    notifications: vi.fn().mockResolvedValue({ items: notifications, unread_count: 1 }),
    notificationPreferences: vi.fn().mockResolvedValue({ enabled: true, muted_event_codes: [], revision: 3 }),
    markNotificationRead: vi.fn().mockResolvedValue({ ...notifications[0], read_at: "2026-07-29T01:03:00Z" }),
    markAllNotificationsRead: vi.fn().mockResolvedValue({ marked_count: 1 }),
    updateNotificationPreferences: vi.fn().mockResolvedValue({ enabled: false, muted_event_codes: [], revision: 4 }),
  } as unknown as ApiClient;
}

describe("NotificationCenterView", () => {
  it("loads notifications, filters actionable items, and navigates from an action", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("通知中心");
    expect(wrapper.text()).toContain("任务已完成");
    expect(wrapper.text()).toContain("已聚合 2 次");
    expect(wrapper.find(".notification-count").text()).toBe("1");

    await wrapper.get(".segmented button:nth-child(3)").trigger("click");
    expect(wrapper.findAll(".notification-item")).toHaveLength(4);

    await wrapper.get(".notification-action .text-button").trigger("click");
    expect(wrapper.emitted("navigate")).toEqual([["workflows"]]);
    expect(api.markNotificationRead).toHaveBeenCalledWith("notification-1");
  });

  it("marks all notifications read and persists preference changes", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    await wrapper.get(".secondary-button").trigger("click");
    await flushPromises();
    expect(api.markAllNotificationsRead).toHaveBeenCalledOnce();
    expect(wrapper.find(".notification-count").exists()).toBe(false);

    await wrapper.get(".notification-preference input").setValue(false);
    await flushPromises();
    expect(api.updateNotificationPreferences).toHaveBeenCalledWith({ enabled: false, revision: 3 });
  });

  it("shows only error and security notifications in the error filter", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    await wrapper.get(".segmented button:nth-child(4)").trigger("click");
    expect(wrapper.findAll(".notification-item")).toHaveLength(1);
    expect(wrapper.text()).toContain("连接需要重新授权");
    expect(wrapper.text()).not.toContain("任务已完成");
  });

  it("navigates review notifications to the organization workbench", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    await wrapper.get(".segmented button:nth-child(3)").trigger("click");
    const reviewItem = wrapper.findAll(".notification-item")[2];
    await reviewItem.get(".notification-action .text-button").trigger("click");

    expect(wrapper.emitted("navigate")).toContainEqual(["organization-plans"]);
    expect(api.markNotificationRead).toHaveBeenCalledWith("notification-3");
  });

  it("shows label and navigates auto-paused subscription notifications to subscription management", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    await wrapper.get(".segmented button:nth-child(3)").trigger("click");
    const subItem = wrapper.findAll(".notification-item")[3];
    expect(subItem.text()).toContain("查看订阅");
    await subItem.get(".notification-action .text-button").trigger("click");

    expect(wrapper.emitted("navigate")).toContainEqual(["subscriptions"]);
    expect(api.markNotificationRead).toHaveBeenCalledWith("notification-4");
  });

  it("persists quiet-hour controls", async () => {
    const api = makeApi();
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    await wrapper.findAll(".notification-preference input")[1].setValue(false);
    await flushPromises();

    expect(api.updateNotificationPreferences).toHaveBeenCalledWith({
      quiet_hours_enabled: false,
      quiet_hours_start: "23:00",
      quiet_hours_end: "08:00",
      quiet_hours_timezone: "Asia/Shanghai",
      error_bypass_quiet_hours: true,
      revision: 3,
    });
  });

  it("keeps the last notification list visible when a refresh fails", async () => {
    const api = makeApi();
    const notificationsRequest = api.notifications as ReturnType<typeof vi.fn>;
    notificationsRequest
      .mockResolvedValueOnce({ items: notifications, unread_count: 1 })
      .mockRejectedValueOnce(new ApiError("通知中心暂时无法加载", 503, "notifications_unavailable"));
    const wrapper = mount(NotificationCenterView, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("任务已完成");
    await wrapper.get(".icon-button").trigger("click");
    await flushPromises();

    expect(wrapper.get(".settings-state-error").text()).toContain("通知中心暂时无法加载");
    expect(wrapper.text()).toContain("任务已完成");
    expect(wrapper.findAll(".notification-item")).toHaveLength(4);
  });
});
