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
  });
});
