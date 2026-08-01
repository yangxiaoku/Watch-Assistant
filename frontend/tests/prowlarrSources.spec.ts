import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import SourceDiagnostics from "../src/components/SourceDiagnostics.vue";
import ResourceTable from "../src/components/ResourceTable.vue";
import { resourceSourceLabel, sourceNameList } from "../src/resourceSources";

const resource = {
  resource_id: "resource-1",
  kind: "magnet" as const,
  name: "示例资源 2026 1080p",
  size_bytes: null,
  seeders: null,
  source: "plugin:prowlarr",
  captured_at: "2026-08-01T10:00:00Z",
};

describe("Prowlarr source presentation", () => {
  it("normalizes source ids without exposing upstream names or credentials", () => {
    expect(sourceNameList(["plugin:pansou", "plugin:prowlarr", "prowlarr"])).toEqual(["PanSou", "Prowlarr"]);
    expect(resourceSourceLabel(resource)).toBe("Prowlarr");
  });

  it("shows a Chinese single-source fallback message from stable warning codes", () => {
    const wrapper = mount(SourceDiagnostics, {
      props: { warnings: ["pansou_query_failed:0", "prowlarr_query_failed:0"] },
    });

    expect(wrapper.get(".source-diagnostics").text()).toContain("部分搜索来源暂不可用");
    expect(wrapper.text()).toContain("PanSou 查询失败");
    expect(wrapper.text()).toContain("Prowlarr 查询失败");
    expect(wrapper.text()).toContain("已保留其他来源的有效结果");
  });

  it("renders the aggregate source count from the confirmed search task field", () => {
    const wrapper = mount(ResourceTable, {
      props: {
        resources: [resource],
        sourceNames: ["plugin:pansou", "plugin:prowlarr"],
        onPush: () => undefined,
      },
    });

    expect(wrapper.get(".resource-source-summary").text()).toBe("来源 2 个：PanSou / Prowlarr");
    expect(wrapper.get(".source-badge").text()).toBe("Prowlarr");
  });
});
