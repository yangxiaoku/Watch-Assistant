import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import ResourceTable from "../src/components/ResourceTable.vue";
import type { ResourceSummary } from "../src/types";

const resource: ResourceSummary = {
  resource_id: "res_1",
  kind: "magnet",
  name: "Inception 2010 2160p",
  size_bytes: null,
  seeders: null,
  source: "plugin:test",
  captured_at: "2026-07-23T10:30:00Z",
};

const facets = { magnet: 500, share: 1, "4k": 100, "1080p": 300, "720p": 50, subtitle: 80 } as const;

describe("ResourceTable", () => {
  it("renders backend items and facet counts without truncating or deriving tags", () => {
    const resources = Array.from({ length: 31 }, (_, index) => ({ ...resource, resource_id: `magnet-${index}`, name: `Magnet ${index}` }));
    resources.push({ ...resource, resource_id: "share-1", kind: "115_share", name: "115 分享" });
    const wrapper = mount(ResourceTable, {
      props: { resources, facets, total: 501, page: 1, totalPages: 21, pageSize: 25, onPush: vi.fn() },
    });

    expect(wrapper.findAll("tbody tr")).toHaveLength(32);
    expect(wrapper.get("h2").text()).toContain("501");
    expect(wrapper.get('[aria-label="资源质量筛选"]').text()).toContain("4K/2160P 100");
    expect(wrapper.get('[aria-label="资源质量筛选"]').text()).toContain("字幕 80");
  });

  it("emits server-side filter, sort, query, page size and page changes", async () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [resource], total: 501, page: 2, totalPages: 21, pageSize: 25, onPush: vi.fn() },
    });
    await wrapper.get('[aria-label="资源类型"] button:nth-child(2)').trigger("click");
    await wrapper.get('[aria-label="资源质量筛选"] button:nth-child(3)').trigger("click");
    await wrapper.get('[aria-label="资源名称搜索"]').setValue(" 1080p ");
    await wrapper.get('[aria-label="资源排序"]').setValue("relevance");
    await wrapper.get('[aria-label="资源每页数量"]').setValue("50");
    await wrapper.get('[aria-label="下一页"]').trigger("click");

    expect(wrapper.emitted("kind")).toEqual([["magnet"]]);
    expect(wrapper.emitted("quality")).toEqual([["1080p"]]);
    expect(wrapper.emitted("query")).toEqual([[" 1080p "]]);
    expect(wrapper.emitted("sort")).toEqual([["relevance"]]);
    expect(wrapper.emitted("pageSize")).toEqual([[50]]);
    expect(wrapper.emitted("page")).toEqual([[3]]);
  });

  it("keeps pagination controls bounded and disables them while loading", () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [resource], total: 501, page: 21, totalPages: 21, resourceLoading: true, onPush: vi.fn() },
    });
    expect(wrapper.get('[aria-label="第一页"]').attributes("disabled")).toBeDefined();
    expect(wrapper.get('[aria-label="下一页"]').attributes("disabled")).toBeDefined();
    expect(wrapper.get(".resource-surface").attributes("aria-busy")).toBe("true");
  });

  it("separates resource search loading from pagination loading", () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [], total: 0, resourceSearchLoading: true, onPush: vi.fn() },
    });

    expect(wrapper.get(".resource-surface").attributes("aria-busy")).toBe("true");
    expect(wrapper.get('[role="status"]').text()).toContain("正在搜索资源");
    expect(wrapper.text()).not.toContain("正在加载资源分页");
  });

  it("does not show an empty state together with a resource error", () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [], resourceError: "资源搜索失败，请重试", onPush: vi.fn() },
    });

    expect(wrapper.get('[role="alert"]').text()).toContain("资源搜索失败，请重试");
    expect(wrapper.find(".empty-state").exists()).toBe(false);
  });

  it("shows a neutral Chinese notice when the resource snapshot is unavailable", () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [resource], paginationUnavailable: true, total: 1, totalPages: 1, onPush: vi.fn() },
    });

    expect(wrapper.get(".resource-page-notice").attributes("role")).toBe("status");
    expect(wrapper.get(".resource-page-notice").text()).toContain("分页暂不可用");
    expect(wrapper.find(".resource-page-error").exists()).toBe(false);
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    expect(wrapper.find(".resource-pagination").exists()).toBe(false);
  });

  it("preserves unknown content fields and only labels verified sources", () => {
    const wrapper = mount(ResourceTable, {
      props: {
        resources: [resource, { ...resource, resource_id: "verified", size_bytes: 1024, size_source: "inspection", inspection_status: "verified", video_file_count: 1, subtitle_count: 0, sample_count: 1 }],
        onPush: vi.fn(),
      },
    });
    expect(wrapper.text()).toContain("已验证");
    expect(wrapper.text()).toContain("未知");
    expect(wrapper.text()).not.toContain("综合 未知");
    expect(wrapper.text()).not.toContain("综合 0.0");
  });
});
