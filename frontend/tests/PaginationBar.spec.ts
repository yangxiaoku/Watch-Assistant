import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import PaginationBar from "../src/components/PaginationBar.vue";

describe("PaginationBar", () => {
  it("marks the control busy and disables every page action while loading", () => {
    const wrapper = mount(PaginationBar, {
      props: { page: 2, totalPages: 3, totalResults: 60, loading: true },
    });

    expect(wrapper.get("nav").attributes("aria-busy")).toBe("true");
    expect(wrapper.findAll("button").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
    expect(wrapper.text()).toContain("正在加载");
  });

  it("caps the last page at 500 and disables forward navigation there", () => {
    const wrapper = mount(PaginationBar, {
      props: { page: 500, totalPages: 900, totalResults: 18000 },
    });

    expect(wrapper.text()).toContain("第 500 / 500 页");
    expect(wrapper.get('button[aria-label="下一页"]').attributes("disabled")).toBeDefined();
    expect(wrapper.get('button[aria-label="最后一页"]').attributes("disabled")).toBeDefined();
  });
});
