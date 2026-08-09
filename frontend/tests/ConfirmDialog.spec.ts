import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ConfirmDialog from "../src/components/ConfirmDialog.vue";

describe("ConfirmDialog", () => {
  it("requires acknowledgement before emitting confirmation", async () => {
    const wrapper = mount(ConfirmDialog, {
      props: { open: true, title: "确认整理", summary: "将提交受控整理操作。", details: ["预计移动：1 项"] },
    });
    const confirm = wrapper.get(".confirm-dialog-actions button:last-child");

    expect(confirm.attributes("disabled")).toBeDefined();
    await wrapper.get(".confirm-dialog-acknowledgement input").setValue(true);
    expect(confirm.attributes("disabled")).toBeUndefined();
    await confirm.trigger("click");
    expect(wrapper.emitted("confirm")).toHaveLength(1);
  });

  it("skips the acknowledgment checkbox for non-destructive operations", async () => {
    const wrapper = mount(ConfirmDialog, {
      props: { open: true, title: "确认取消", summary: "只取消尚未开始的任务。", requireAcknowledgment: false },
    });
    expect(wrapper.find(".confirm-dialog-acknowledgement").exists()).toBe(false);
    const confirm = wrapper.get(".confirm-dialog-actions button:last-child");
    expect(confirm.attributes("disabled")).toBeUndefined();
    await confirm.trigger("click");
    expect(wrapper.emitted("confirm")).toHaveLength(1);
  });

  it("closes on Escape without confirming", async () => {
    const wrapper = mount(ConfirmDialog, {
      props: { open: true, title: "确认清理", summary: "将提交可恢复操作。" },
    });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("cancel")).toHaveLength(1);
    expect(wrapper.emitted("confirm")).toBeUndefined();
  });
});
