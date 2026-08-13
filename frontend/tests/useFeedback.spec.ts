import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFeedback } from "../src/composables/useFeedback";

describe("useFeedback", () => {
  beforeEach(() => {
    useFeedback().clear();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("pushes and dismisses toasts with level and message", () => {
    const fb = useFeedback();
    fb.success("已保存");
    fb.error("推送失败");
    expect(fb.toasts.value.map((t) => [t.level, t.message])).toEqual([
      ["success", "已保存"],
      ["error", "推送失败"],
    ]);
    fb.dismiss(fb.toasts.value[0].id);
    expect(fb.toasts.value.map((t) => t.message)).toEqual(["推送失败"]);
  });

  it("deduplicates identical level+message within 2s", () => {
    const fb = useFeedback();
    fb.error("推送失败");
    fb.error("推送失败");
    expect(fb.toasts.value).toHaveLength(1);
    vi.advanceTimersByTime(2100);
    fb.error("推送失败");
    expect(fb.toasts.value).toHaveLength(2);
  });

  it("does not deduplicate same message across levels", () => {
    const fb = useFeedback();
    fb.warning("设置未保存");
    fb.error("设置未保存");
    expect(fb.toasts.value).toHaveLength(2);
  });

  it("caps the stack at 4 toasts", () => {
    const fb = useFeedback();
    for (let i = 0; i < 6; i++) fb.info(`消息 ${i}`);
    expect(fb.toasts.value).toHaveLength(4);
    expect(fb.toasts.value[0].message).toBe("消息 2");
  });

  it("clears all toasts", () => {
    const fb = useFeedback();
    fb.success("a");
    fb.warning("b");
    fb.clear();
    expect(fb.toasts.value).toHaveLength(0);
  });

  it("carries action label and callback", () => {
    const fb = useFeedback();
    const onAction = vi.fn();
    fb.success("已推送", { actionLabel: "查看推送记录", onAction });
    const item = fb.toasts.value[0];
    expect(item.actionLabel).toBe("查看推送记录");
    item.onAction?.();
    expect(onAction).toHaveBeenCalledOnce();
  });
});
