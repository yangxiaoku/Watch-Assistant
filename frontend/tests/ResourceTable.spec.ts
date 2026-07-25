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
    const names = wrapper.findAll("tbody .resource-title").map((cell) => cell.text());
    expect(names.slice(0, 2)).toEqual(["磁力 0", "磁力 1"]);
    expect(names).toContain("115 分享");
  });

  it("labels PanSou metrics and keeps more and retry actions independent", async () => {
    const wrapper = mount(ResourceTable, {
      props: {
        resources: [{ ...magnetWithoutStats, size_bytes: 1024, size_source: "pansou", seeders: 4, seeders_source: "pansou" }],
        inspectionSupported: true,
        inspectionState: "completed",
        inspectionMoreAvailable: true,
        inspectionRetryAvailable: true,
        onPush: vi.fn(),
      },
    });

    expect(wrapper.text()).toContain("来源数据");
    expect(wrapper.text()).toContain("检测更多");
    expect(wrapper.text()).toContain("重试失败项");
    expect(wrapper.text()).not.toContain("检测本页磁力");
    await wrapper.findAll(".inspection-more-button")[0].trigger("click");
    await wrapper.findAll(".inspection-more-button")[1].trigger("click");
    expect(wrapper.emitted("inspectMore")).toHaveLength(1);
    expect(wrapper.emitted("retryFailed")).toHaveLength(1);
  });

  it("remembers the selected resource sort", async () => {
    const wrapper = mount(ResourceTable, {
      props: { resources: [magnetWithoutStats], onPush: vi.fn() },
    });

    await wrapper.get('select[aria-label="资源排序"]').setValue("size");
    expect(window.localStorage.getItem("watch-assistant:resource-sort")).toBe("size");
  });

  it("filters names by inferred quality tags", async () => {
    const wrapper = mount(ResourceTable, {
      props: {
        resources: [
          { ...magnetWithoutStats, resource_id: "4k", name: "Film 2160P" },
          { ...magnetWithoutStats, resource_id: "hd", name: "Film 1080P 字幕" },
          { ...magnetWithoutStats, resource_id: "sd", name: "Film 720P" },
        ],
        onPush: vi.fn(),
      },
    });

    expect(wrapper.get('[aria-label="资源名称标签"]').text()).toContain("4K/2160P 1");
    await wrapper.get('[aria-label="资源名称标签"] button[aria-pressed="false"]').trigger("click");
    expect(wrapper.findAll("tbody tr")).toHaveLength(1);
    expect(wrapper.get(".resource-title").text()).toContain("2160P");
  });

  it("matches subtitle release tokens without matching ordinary words", async () => {
    const subtitleNames = ["Movie.CHS.1080p", "Movie.Subtitle", "Movie.简中"];
    const resources: ResourceSummary[] = [
      ...subtitleNames.map((name, index) => ({ ...magnetWithoutStats, resource_id: `subtitle-${index}`, name })),
      { ...magnetWithoutStats, resource_id: "submarine", name: "Submarine.2025" },
      { ...magnetWithoutStats, resource_id: "substance", name: "The.Substance.2024" },
    ];
    const wrapper = mount(ResourceTable, {
      props: { resources, onPush: vi.fn() },
    });

    expect(wrapper.get('[aria-label="资源名称标签"]').text()).toContain("字幕 3");
    await wrapper.get('[aria-label="资源名称标签"] button:nth-child(5)').trigger("click");
    expect(wrapper.findAll("tbody tr")).toHaveLength(3);
    expect(wrapper.findAll(".resource-title").map((cell) => cell.text())).toEqual(subtitleNames);
  });
});
