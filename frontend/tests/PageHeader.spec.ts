import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import PageHeader from "../src/components/PageHeader.vue";
import SegmentedControl from "../src/components/SegmentedControl.vue";

describe("PageHeader", () => {
  it("渲染衬线标题与 eyebrow", () => {
    const wrapper = mount(PageHeader, { props: { eyebrow: "浏览 TMDB", title: "电影库" } });
    expect(wrapper.get("p.eyebrow").text()).toBe("浏览 TMDB");
    expect(wrapper.get("h1").text()).toBe("电影库");
  });

  it("渲染 titleId 并透传操作区 slot", () => {
    const wrapper = mount(PageHeader, {
      props: { title: "设置", titleId: "settings-title" },
      slots: { actions: "<button>保存</button>", description: "<p>说明</p>" },
    });
    expect(wrapper.get("h1").attributes("id")).toBe("settings-title");
    expect(wrapper.get(".page-header-actions").text()).toContain("保存");
    expect(wrapper.text()).toContain("说明");
  });
});

describe("SegmentedControl", () => {
  const options = [
    { value: "popular", label: "热度优先" },
    { value: "rating", label: "评分优先" },
  ];

  it("渲染选项并高亮当前值", () => {
    const wrapper = mount(SegmentedControl, { props: { options, modelValue: "rating" } });
    expect(wrapper.findAll("button").length).toBe(2);
    expect(wrapper.get('button[aria-selected="true"]').text()).toBe("评分优先");
  });

  it("点击选项发出 update:modelValue", async () => {
    const wrapper = mount(SegmentedControl, { props: { options, modelValue: "popular" } });
    await wrapper.get("button:nth-child(2)").trigger("click");
    expect(wrapper.emitted("update:modelValue")?.[0]).toEqual(["rating"]);
  });
});
