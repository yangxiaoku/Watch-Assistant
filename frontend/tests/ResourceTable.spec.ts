import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import ResourceTable from "../src/components/ResourceTable.vue";
import type { ResourceSummary } from "../src/types";

const magnetWithoutStats: ResourceSummary = {
  resource_id: "res_1",
  kind: "magnet",
  name: "Inception 2010 2160p",
  size_bytes: null,
  seeders: null,
  source: "plugin:thepiratebay",
  captured_at: "2026-07-23T10:30:00Z",
};

describe("ResourceTable", () => {
  it("renders missing size and seeders as unknown", () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [magnetWithoutStats], onPush: vi.fn() },
    });

    expect(wrapper.text()).toContain("未知");
    expect(wrapper.text()).toContain("plugin:thepiratebay");
    expect(wrapper.text()).not.toContain("综合 未知");
    expect(wrapper.text()).not.toContain("综合 0.0");
    expect(wrapper.findAll(".quality-metrics")).toHaveLength(0);
  });

  it("keeps shares, caps magnets at 30, and sorts ties stably", async () => {
    const resources: ResourceSummary[] = Array.from({ length: 31 }, (_, index) => ({
      ...magnetWithoutStats,
      resource_id: `magnet_${index}`,
      name: `磁力 ${index}`,
      relevance_score: index < 2 ? 0.8 : null,
    }));
    resources.push({
      ...magnetWithoutStats,
      resource_id: "share_1",
      kind: "115_share",
      name: "115 分享",
    });

    const wrapper = mount(ResourceTable, {
      props: { resources, onPush: vi.fn() },
    });

    expect(wrapper.findAll("tbody tr")).toHaveLength(31);
    expect(wrapper.text()).toContain("115 分享");

    await wrapper.get('select[aria-label="资源排序"]').setValue("relevance");
    const names = wrapper.findAll("tbody .resource-name").map((cell) => cell.text());
    expect(names.slice(0, 2)).toEqual(["磁力 0", "磁力 1"]);
    expect(names).toContain("115 分享");
  });
});
