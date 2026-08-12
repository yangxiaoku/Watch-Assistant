import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api";
import type { SubscriptionResponse } from "../src/types";
import SubscriptionView from "../src/views/SubscriptionView.vue";

function subscription(overrides: Partial<SubscriptionResponse> = {}): SubscriptionResponse {
  return {
    id: "sub-1",
    tmdb_id: 27205,
    media_type: "movie",
    season_number: null,
    episode_start: null,
    episode_end: null,
    mode: "remind",
    status: "active",
    quality_profile_id: null,
    next_check_at: null,
    last_checked_at: "2026-08-01T00:00:00Z",
    last_match_count: 0,
    last_error_code: null,
    revision: 1,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function makeApi(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    subscriptions: vi.fn().mockResolvedValue([subscription()]),
    createSubscription: vi.fn().mockResolvedValue(subscription()),
    subscriptionCheck: vi.fn().mockResolvedValue({ subscription_id: "sub-1", status: "active", next_check_at: null, new_matches: 0 }),
    subscriptionPause: vi.fn().mockResolvedValue(subscription({ status: "paused" })),
    subscriptionResume: vi.fn().mockResolvedValue(subscription({ status: "active" })),
    subscriptionObservations: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
}

describe("SubscriptionView", () => {
  it("loads and renders subscriptions", async () => {
    const api = makeApi();
    const wrapper = mount(SubscriptionView, { props: { api } });
    await flushPromises();
    expect(api.subscriptions).toHaveBeenCalled();
    expect(wrapper.text()).toContain("TMDB #27205");
    expect(wrapper.text()).toContain("活跃");
    expect(wrapper.text()).toContain("提醒");
  });

  it("creates a subscription from the form", async () => {
    const api = makeApi();
    const wrapper = mount(SubscriptionView, { props: { api } });
    await flushPromises();
    await wrapper.find("button.primary").trigger("click"); // 打开表单
    const inputs = wrapper.findAll("input");
    await inputs[0].setValue("12345");
    const selects = wrapper.findAll("select");
    await selects[0].setValue("tv");
    await wrapper.find("form.create-form").trigger("submit");
    await flushPromises();
    expect(api.createSubscription).toHaveBeenCalledWith({
      tmdb_id: 12345,
      media_type: "tv",
      mode: "remind",
    });
  });

  it("triggers an immediate check", async () => {
    const api = makeApi();
    const wrapper = mount(SubscriptionView, { props: { api } });
    await flushPromises();
    const checkButton = wrapper.findAll("button").find((b) => b.text().includes("检查"));
    expect(checkButton).toBeTruthy();
    await checkButton!.trigger("click");
    await flushPromises();
    expect(api.subscriptionCheck).toHaveBeenCalledWith("sub-1");
  });

  it("pauses and resumes a subscription", async () => {
    const api = makeApi();
    const wrapper = mount(SubscriptionView, { props: { api } });
    await flushPromises();
    const pauseButton = wrapper.findAll("button").find((b) => b.text().includes("暂停"));
    await pauseButton!.trigger("click");
    await flushPromises();
    expect(api.subscriptionPause).toHaveBeenCalledWith("sub-1", 1);
  });

  it("shows observations for a subscription", async () => {
    const api = makeApi({
      subscriptionObservations: vi.fn().mockResolvedValue([
        { resource_id: "res-1", captured_at: "2026-08-01T00:00:00Z", source: "pansou", name: "Movie.2026.1080p.mkv", size_bytes: 1024 },
      ]),
    });
    const wrapper = mount(SubscriptionView, { props: { api } });
    await flushPromises();
    const resourcesButton = wrapper.findAll("button").find((b) => b.text().includes("资源"));
    await resourcesButton!.trigger("click");
    await flushPromises();
    expect(api.subscriptionObservations).toHaveBeenCalledWith("sub-1");
    expect(wrapper.text()).toContain("Movie.2026.1080p.mkv");
  });
});
