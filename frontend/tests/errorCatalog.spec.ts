import { describe, expect, it } from "vitest";

import { describeUiError, taskErrorMessage, UI_ERROR_CODES } from "../src/errorCatalog";

describe("中文错误目录", () => {
  it("provides an actionable structured message for known errors", () => {
    expect(describeUiError("settings_conflict", 409)).toMatchObject({
      title: "设置已在其他位置更新",
      message: "本次修改未保存，当前页面不是最新版本。",
      suggestion: "请加载最新设置后重新提交。",
      retryable: false,
      action: "reload_settings",
    });
  });

  it("does not expose an unknown backend detail", () => {
    const error = describeUiError("python_exception=secret", 500);
    expect(error.message).not.toContain("python_exception");
    expect(error.message).toContain("本次操作未完成");
    expect(error.retryable).toBe(true);
  });

  it("retries transient QR provider responses", () => {
    expect(describeUiError("qrcode_provider_unavailable", 503)).toMatchObject({
      retryable: true,
      action: "retry",
    });
  });

  it("maps task error codes without rendering raw error text", () => {
    expect(taskErrorMessage("uncertain")).toContain("不要重复操作");
    expect(taskErrorMessage("raw_backend_exception")).toContain("操作暂时无法完成");
    expect(taskErrorMessage(null)).toBeNull();
  });

  it("explains inventory push gates and opens the media library", () => {
    expect(describeUiError("inventory_scope_unconfigured")).toMatchObject({
      title: "未配置 115 媒体库范围",
      message: "本次推送未提交到 115，系统还没有可核对的媒体库库存范围。",
      action: "open_library",
      retryable: false,
    });
    expect(taskErrorMessage("inventory_scope_unconfigured")).toContain("完成一次完整扫描");
  });

  it("keeps actions available for backend operational codes", () => {
    expect(UI_ERROR_CODES).toContain("cache_warm_disabled");
    expect(describeUiError("cache_warm_disabled")).toMatchObject({
      retryable: false,
      action: "inspect_configuration",
    });
    expect(describeUiError("uncertain_requires_verification")).toMatchObject({
      retryable: false,
      action: "view_task",
    });
    expect(describeUiError("resource_snapshot_not_found")).toMatchObject({
      retryable: true,
      action: "refresh_snapshot",
    });
  });
});
