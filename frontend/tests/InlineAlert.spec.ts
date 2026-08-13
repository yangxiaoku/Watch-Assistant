import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import InlineAlert from "../src/components/InlineAlert.vue";

describe("InlineAlert", () => {
  it("renders message with the variant class", () => {
    const wrapper = mount(InlineAlert, { props: { variant: "error", message: "加载失败" } });
    expect(wrapper.find(".inline-alert-error").exists()).toBe(true);
    expect(wrapper.text()).toContain("加载失败");
  });

  it("renders title and message together", () => {
    const wrapper = mount(InlineAlert, { props: { variant: "warning", title: "设置冲突", message: "已在其他位置更新" } });
    expect(wrapper.text()).toContain("设置冲突");
    expect(wrapper.text()).toContain("已在其他位置更新");
  });

  it("emits action when the action button is clicked", async () => {
    const wrapper = mount(InlineAlert, {
      props: { variant: "error", message: "加载失败", actionLabel: "重试" },
    });
    await wrapper.find(".inline-alert-action").trigger("click");
    expect(wrapper.emitted("action")).toHaveLength(1);
  });

  it("emits close when closable and the close button is clicked", async () => {
    const wrapper = mount(InlineAlert, {
      props: { variant: "info", message: "已刷新", closable: true },
    });
    await wrapper.find(".inline-alert-close").trigger("click");
    expect(wrapper.emitted("close")).toHaveLength(1);
  });

  it("does not render action button without actionLabel", () => {
    const wrapper = mount(InlineAlert, { props: { variant: "success", message: "已保存" } });
    expect(wrapper.find(".inline-alert-action").exists()).toBe(false);
  });

  it("renders default slot content", () => {
    const wrapper = mount(InlineAlert, { props: { variant: "info" }, slots: { default: "自定义内容" } });
    expect(wrapper.text()).toContain("自定义内容");
  });
});
