import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ToastStack from "../src/components/ToastStack.vue";
import { useFeedback } from "../src/composables/useFeedback";

describe("ToastStack", () => {
  beforeEach(() => {
    useFeedback().clear();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders toasts with level classes and messages", () => {
    const fb = useFeedback();
    fb.success("设置已保存");
    fb.error("推送失败");
    const wrapper = mount(ToastStack);
    expect(wrapper.findAll(".toast")).toHaveLength(2);
    expect(wrapper.find(".toast-success .toast-message").text()).toBe("设置已保存");
    expect(wrapper.find(".toast-error .toast-message").text()).toBe("推送失败");
  });

  it("auto-dismisses success after 3.5s but keeps error", async () => {
    const fb = useFeedback();
    fb.success("设置已保存");
    fb.error("推送失败");
    const wrapper = mount(ToastStack);
    expect(wrapper.findAll(".toast")).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(3600);
    expect(wrapper.findAll(".toast")).toHaveLength(1);
    expect(wrapper.find(".toast-message").text()).toBe("推送失败");
  });

  it("auto-dismisses warning after 5s", async () => {
    const fb = useFeedback();
    fb.warning("即将到期");
    const wrapper = mount(ToastStack);
    await vi.advanceTimersByTimeAsync(5100);
    expect(wrapper.findAll(".toast")).toHaveLength(0);
  });

  it("dismisses on close button click", async () => {
    const fb = useFeedback();
    fb.error("推送失败");
    const wrapper = mount(ToastStack);
    await wrapper.find(".toast-close").trigger("click");
    expect(wrapper.findAll(".toast")).toHaveLength(0);
  });

  it("runs the action callback and dismisses on action click", async () => {
    const fb = useFeedback();
    const onAction = vi.fn();
    fb.success("已推送", { actionLabel: "查看推送记录", onAction });
    const wrapper = mount(ToastStack);
    await wrapper.find(".toast-action").trigger("click");
    expect(onAction).toHaveBeenCalledOnce();
    expect(wrapper.findAll(".toast")).toHaveLength(0);
  });

  it("removes timers when toasts are dismissed externally", async () => {
    const fb = useFeedback();
    fb.success("设置已保存");
    const wrapper = mount(ToastStack);
    fb.dismiss(fb.toasts.value[0].id);
    await vi.advanceTimersByTimeAsync(4000);
    expect(wrapper.findAll(".toast")).toHaveLength(0);
  });
});
