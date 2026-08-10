import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { ApiClient, ApiError } from "../src/api";
import type { ResourceSearchResponse } from "../src/types";

describe("App capability wiring", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it("renders the merged organization view with execution capability", async () => {
    window.history.replaceState({}, "", "/organization");
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
      organization_execution_supported: true,
    });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();
    vi.spyOn(ApiClient.prototype, "organizationSettings").mockResolvedValue({
      schedule_enabled: false,
      auto_execute_enabled: false,
      scan_interval_minutes: 30,
      source_directory_ids: [],
      source_directory_labels: [],
      target_directory_id: null,
      target_directory_label: null,
      push_directory_id: null,
      push_directory_label: null,
      video_extensions: ["mkv"],
      metadata_extensions: ["srt"],
      rename_enabled: true,
      media_probe_enabled: true,
      ai_identification_enabled: false,
      small_file_threshold_mb: 0,
      cleanup_empty_directories: false,
      strm_linkage_enabled: false,
      operation_delay_seconds: 1.5,
      include_children_category: false,
      include_concert_category: false,
      region_grouping_enabled: true,
      year_grouping_enabled: false,
      prefer_remux: true,
      prefer_resolution: true,
      prefer_dolby: false,
      conflict_mode: 2,
      multi_version_enabled: false,
      revision: 0,
    });
    vi.spyOn(ApiClient.prototype, "organizationResult").mockResolvedValue({
      status: "unknown",
      available_statuses: ["unknown"],
      source_count: 0,
      scanned_count: 0,
      plan_count: 0,
      queued_count: 0,
      blocked_count: 0,
      blocked_details: [],
      items: [],
      finished_at: null,
      run_id: null,
    });
    vi.spyOn(ApiClient.prototype, "organizationPlans").mockResolvedValue({ items: [plan], next_cursor: null });
    vi.spyOn(ApiClient.prototype, "organizationPlanOperation").mockResolvedValue(null);

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("开始整理");
    expect(wrapper.text()).toContain("停止定时");
    expect(wrapper.text()).toContain("待处理");
    wrapper.unmount();
  });

  it("does not render an empty search view on the home page", async () => {
    window.history.replaceState({}, "", "/");
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({
      status: "ok",
      release: "test",
      push_supported: false,
    });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();
    vi.spyOn(ApiClient.prototype, "homeCatalog").mockResolvedValue({
      popular: [],
      now_playing: [],
      upcoming: [],
      top_rated: [],
      tv_popular: [],
      tv_on_the_air: [],
      tv_top_rated: [],
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find(".home-empty-state").exists()).toBe(true);
    expect(wrapper.find(".search-view").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("没有找到相关影视");
    wrapper.unmount();
  });

  it("falls back to home when the organization capability is disabled", async () => {
    window.history.replaceState({}, "", "/organization");
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({
      status: "ok",
      release: "test",
      push_supported: false,
      organization_plan_enabled: false,
      organization_execution_enabled: false,
      organization_execution_supported: false,
      strm_capabilities: { full: false, incremental: false, cleanup: false, playback: false, playback_contract_verified: false },
    });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();
    vi.spyOn(ApiClient.prototype, "homeCatalog").mockResolvedValue({
      popular: [],
      now_playing: [],
      upcoming: [],
      top_rated: [],
      tv_popular: [],
      tv_on_the_air: [],
      tv_top_rated: [],
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find(".home-empty-state").exists()).toBe(true);
    expect(wrapper.find(".organization-view").exists()).toBe(false);
    wrapper.unmount();
  });

  it("keeps homepage catalog failures in a neutral retryable empty state", async () => {
    const home = vi.spyOn(ApiClient.prototype, "homeCatalog")
      .mockRejectedValueOnce(new ApiError("本次影视资料没有更新。", 503, "tmdb_unavailable"))
      .mockResolvedValueOnce({
        popular: [{ tmdb_id: 27205, media_type: "movie", title: "盗梦空间", original_title: "Inception", release_year: 2010, overview: "梦境中的梦境。", poster_path: null, backdrop_path: null, genre_ids: [], vote_average: 8.4 }],
        now_playing: [],
        upcoming: [],
        top_rated: [],
        tv_popular: [],
        tv_on_the_air: [],
        tv_top_rated: [],
      });
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({ status: "ok", release: "test", push_supported: false });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue();

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find(".error-strip").exists()).toBe(false);
    expect(wrapper.get(".home-empty-state").text()).toContain("首页资料暂时不可用");
    expect(wrapper.get(".home-empty-state").text()).toContain("本次影视资料没有更新");
    await wrapper.get(".home-empty-state button").trigger("click");
    await flushPromises();

    expect(home).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".feature-hero").text()).toContain("盗梦空间");
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
      organization_execution_supported: false,
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

  it("retries metadata with the media type and TMDB id in the API order", async () => {
    window.history.replaceState({}, "", "/movie/27205");
    const metadata = {
      tmdb_id: 27205,
      media_type: "movie" as const,
      title: "测试电影",
      original_title: "Test Movie",
      release_year: 2026,
      overview: "测试简介",
      poster_path: null,
      backdrop_path: null,
      genre_ids: [],
      vote_average: 7.5,
    };
    const mediaMetadata = vi.spyOn(ApiClient.prototype, "mediaMetadata")
      .mockRejectedValueOnce(new ApiError("资料暂时不可用", 503, "tmdb_unavailable"))
      .mockResolvedValueOnce(metadata);
    vi.spyOn(ApiClient.prototype, "recordMediaDetailMetric").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({ status: "ok", release: "test", push_supported: false });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "startResourceSearch").mockResolvedValue({
      task_id: "resource-search-1",
      tmdb_id: 27205,
      media_type: "movie",
      season_number: null,
      status: "ready",
      snapshot_revision: "snapshot-1",
      query_plan_version: "v1",
      selected_season: null,
      sources: [],
      warnings: [],
      cache_age_seconds: null,
      error_code: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    });
    vi.spyOn(ApiClient.prototype, "resources").mockResolvedValue({
      items: [],
      page: 1,
      page_size: 25,
      total: 0,
      total_pages: 1,
      facets: { magnet: 0, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 },
      snapshot_revision: "snapshot-1",
    });

    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("影视资料暂时无法加载");

    const retryButton = wrapper.findAll("button").find((button) => button.text().includes("重试资料"));
    expect(retryButton).toBeDefined();
    await retryButton!.trigger("click");
    await flushPromises();

    expect(mediaMetadata).toHaveBeenLastCalledWith("movie", 27205, expect.any(AbortSignal));
    expect(wrapper.text()).toContain("测试电影");
    wrapper.unmount();
  });

  it("keeps the current resource page visible while a refresh page is pending", async () => {
    window.history.replaceState({}, "", "/movie/27205");
    const metadata = {
      tmdb_id: 27205,
      media_type: "movie" as const,
      title: "测试电影",
      original_title: "Test Movie",
      release_year: 2026,
      overview: null,
      poster_path: null,
      backdrop_path: null,
      genre_ids: [],
      vote_average: 7.5,
    };
    const resourceSearchResponse = {
      task_id: "resource-search-1",
      tmdb_id: 27205,
      media_type: "movie" as const,
      season_number: null,
      status: "ready" as const,
      snapshot_revision: "snapshot-1",
      query_plan_version: "v1",
      selected_season: null,
      sources: [],
      warnings: [],
      cache_age_seconds: null,
      error_code: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const facets = { magnet: 1, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 };
    const oldResource = { resource_id: "old-resource", kind: "magnet" as const, name: "旧资源", size_bytes: null, seeders: null, source: "test", captured_at: "2026-08-01T00:00:00Z" };
    const newResource = { ...oldResource, resource_id: "new-resource", name: "新资源" };
    const initialPage = { items: [oldResource], page: 1, page_size: 25 as const, total: 1, total_pages: 1, facets, snapshot_revision: "snapshot-1" };
    const refreshedPage = { items: [newResource], page: 1, page_size: 25 as const, total: 1, total_pages: 1, facets, snapshot_revision: "snapshot-2" };
    let resourcePageCalls = 0;
    let releaseRefreshPage: (() => void) | null = null;
    const resources = vi.spyOn(ApiClient.prototype, "resources").mockImplementation(async () => {
      resourcePageCalls += 1;
      if (resourcePageCalls === 2) {
        await new Promise<void>((resolve) => { releaseRefreshPage = resolve; });
        return refreshedPage;
      }
      return initialPage;
    });
    vi.spyOn(ApiClient.prototype, "recordMediaDetailMetric").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({ status: "ok", release: "test", push_supported: false });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "mediaMetadata").mockResolvedValue(metadata);
    vi.spyOn(ApiClient.prototype, "startResourceSearch").mockResolvedValue(resourceSearchResponse);

    const wrapper = mount(App);
    await flushPromises();
    expect(resources).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("旧资源");

    await wrapper.get(".refresh-button").trigger("click");
    await flushPromises();
    expect(resources).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("旧资源");

    releaseRefreshPage?.();
    await flushPromises();
    expect(wrapper.text()).toContain("新资源");
    wrapper.unmount();
  });

  it("keeps the search snapshot visible when the resource page snapshot is missing", async () => {
    window.history.replaceState({}, "", "/movie/27205");
    const metadata = {
      tmdb_id: 27205,
      media_type: "movie" as const,
      title: "测试电影",
      original_title: "Test Movie",
      release_year: 2026,
      overview: null,
      poster_path: null,
      backdrop_path: null,
      genre_ids: [],
      vote_average: 7.5,
    };
    const fallbackResource = { resource_id: "legacy-resource", kind: "magnet" as const, name: "旧搜索快照", size_bytes: null, seeders: null, source: "test", captured_at: "2026-08-01T00:00:00Z" };
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({ status: "ok", release: "test", push_supported: false });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "recordMediaDetailMetric").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "mediaMetadata").mockResolvedValue(metadata);
    vi.spyOn(ApiClient.prototype, "startResourceSearch").mockRejectedValue(new ApiError("资源搜索任务接口不可用", 404, "resource_search_endpoint_unavailable"));
    vi.spyOn(ApiClient.prototype, "search").mockResolvedValue({ movie: metadata, results: [fallbackResource], warnings: [], cached: false, cache_age_seconds: null });
    vi.spyOn(ApiClient.prototype, "resources").mockRejectedValue(new ApiError("当前分页快照已失效", 404, "resource_snapshot_not_found"));

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get(".resource-page-notice").text()).toContain("分页暂不可用");
    expect(wrapper.get(".resource-title").text()).toContain("旧搜索快照");
    expect(wrapper.find(".resource-pagination").exists()).toBe(false);
    expect(wrapper.find(".resource-page-error").exists()).toBe(false);
    wrapper.unmount();
  });

  it("shows search loading separately and avoids an empty state after search failure", async () => {
    window.history.replaceState({}, "", "/movie/27205");
    const metadata = {
      tmdb_id: 27205,
      media_type: "movie" as const,
      title: "测试电影",
      original_title: "Test Movie",
      release_year: 2026,
      overview: null,
      poster_path: null,
      genre_ids: [],
      vote_average: 7.5,
    };
    let resolveSearch!: (value: ResourceSearchResponse) => void;
    const searchPromise = new Promise<ResourceSearchResponse>((resolve) => { resolveSearch = resolve; });
    vi.spyOn(ApiClient.prototype, "recordMediaDetailMetric").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({ status: "ok", release: "test", push_supported: false });
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue(undefined);
    vi.spyOn(ApiClient.prototype, "mediaMetadata").mockResolvedValue(metadata);
    vi.spyOn(ApiClient.prototype, "startResourceSearch").mockReturnValue(searchPromise);

    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("正在搜索资源");
    expect(wrapper.text()).not.toContain("正在加载资源分页");

    resolveSearch({
      task_id: "resource-search-failed",
      tmdb_id: 27205,
      media_type: "movie",
      season_number: null,
      status: "failed",
      snapshot_revision: null,
      query_plan_version: "v1",
      selected_season: null,
      sources: [],
      warnings: [],
      cache_age_seconds: null,
      error_code: "resource_search_failed",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    });
    await flushPromises();

    expect(wrapper.get(".resource-page-error").text()).toContain("本次操作未完成");
    expect(wrapper.find(".empty-state").exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("App login error copy", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  function mountAuthGate() {
    vi.spyOn(ApiClient.prototype, "me").mockRejectedValue(new ApiError("unauthorized", 401, "unauthorized"));
    vi.spyOn(ApiClient.prototype, "health").mockResolvedValue({
      status: "ok",
      release: "test",
      push_supported: false,
      organization_plan_enabled: false,
      organization_execution_enabled: false,
      organization_execution_supported: false,
    });
    return mount(App);
  }

  it("shows a rate-limit message instead of wrong-credentials on 429", async () => {
    vi.spyOn(ApiClient.prototype, "login").mockRejectedValue(new ApiError("rate_limited", 429, "rate_limited"));
    const wrapper = mountAuthGate();
    await flushPromises();

    await wrapper.get("#username").setValue("admin");
    await wrapper.get("#password").setValue("admin");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.get(".error-text").text()).toContain("尝试次数过多");
    wrapper.unmount();
  });

  it("shows wrong-credentials message on a 401", async () => {
    vi.spyOn(ApiClient.prototype, "login").mockRejectedValue(new ApiError("登录信息不正确", 401, "invalid_credentials"));
    const wrapper = mountAuthGate();
    await flushPromises();

    await wrapper.get("#username").setValue("admin");
    await wrapper.get("#password").setValue("wrong");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.get(".error-text").text()).toContain("账号或密码不正确");
    wrapper.unmount();
  });
});
