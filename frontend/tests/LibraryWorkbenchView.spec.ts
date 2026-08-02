import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import LibraryWorkbenchView from "../src/views/LibraryWorkbenchView.vue";

describe("LibraryWorkbenchView", () => {
  it("keeps the library entry reachable while explaining the disabled STRM gate", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mount(LibraryWorkbenchView, {
      props: {
        api: api as never,
        strmFullCapability: { enabled: false, reason_code: "strm_full_disabled", reason_zh: "STRM 全量生成未启用，请检查部署功能开关。", settings_section: "overview" },
      },
    });
    await flushPromises();

    const fullButton = wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"));
    expect(fullButton?.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("STRM 全量生成未启用，请检查部署功能开关。");
    await wrapper.findAll("button").find((button) => button.text().includes("前往设置"))?.trigger("click");
    expect(wrapper.emitted("open-settings")?.at(-1)).toEqual(["overview"]);
  });

  it("shows the first safe setup step when no library is configured", async () => {
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      p115Directories: vi.fn(),
      libraryMedia: vi.fn(),
      strmManifest: vi.fn(),
      strmOperations: vi.fn(),
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    expect(wrapper.text()).toContain("下一步：读取服务器配置的 115 根目录，保存配置、验证范围，再完成首次扫描。");
    await wrapper.findAll("button").find((button) => button.text().includes("检查 115 整理设置"))?.trigger("click");
    expect(wrapper.emitted("open-settings")?.at(-1)).toEqual(["organization"]);
  });

  it("initializes the default library from the configured P115 root", async () => {
    const saved = { library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: false, scope_verified: false, revision: 1, latest_scan: null };
    const verified = { ...saved, enabled: true, scope_verified: true, revision: 2 };
    const scanned = { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 2, added_count: 2, changed_count: 0, removed_count: 0, error_code: null };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      p115Directories: vi.fn().mockResolvedValue({ parent_id: "123", items: [], has_more: false, next_page: null }),
      configureLibrary: vi.fn().mockResolvedValue(saved),
      verifyLibraryScope: vi.fn().mockResolvedValue({ library: verified, verified: true, enabled: true }),
      scanLibrary: vi.fn().mockResolvedValue(scanned),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    const initialize = wrapper.findAll("button").find((button) => button.text().includes("初始化并扫描媒体库"));
    expect(initialize).toBeTruthy();
    await initialize!.trigger("click");
    await flushPromises();

    expect(api.p115Directories).toHaveBeenCalledOnce();
    expect(api.configureLibrary).toHaveBeenCalledWith("main", { name: "115 媒体库", root_directory_id: "123", revision: 0 });
    expect(api.verifyLibraryScope).toHaveBeenCalledWith("main");
    expect(api.scanLibrary).toHaveBeenCalledWith("main");
    expect(wrapper.text()).toContain("现在可以重新推送");
  });

  it("generates STRM after a complete scan and confirms reviewed cleanup", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const plan = { plan_id: "plan-1", library_id: "main", source_scan_run_id: "scan-1", source_snapshot_revision: 1, plan_hash: "a".repeat(64), status: "needs_review" as const, revision: 1, expires_at: "2026-08-01T00:00:00Z", candidate_count: 1, executable_count: 1, blocked_count: 0 };
    const operation = { operation_id: "strm_op_1", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, status: "succeeded" as const, generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: "2026-08-01T00:00:01Z" };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [operation], next_cursor: null }),
      generateStrm: vi.fn().mockResolvedValue({ operation_id: operation.operation_id, library_id: "main", scan_run_id: "scan-1", generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0 }),
      strmOperation: vi.fn().mockResolvedValue(operation),
      createStrmCleanupPlan: vi.fn().mockResolvedValue(plan),
      applyStrmCleanupPlan: vi.fn().mockResolvedValue({ plan: { ...plan, status: "applied", revision: 2 }, retired: 1 }),
    };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await flushPromises();
    expect(api.generateStrm).toHaveBeenCalledWith("main", "scan-1");
    expect(wrapper.text()).toContain("STRM 全量同步完成");

    await wrapper.findAll("button").find((button) => button.text().includes("预览失效清理"))!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("失效清理预览已生成");

    await wrapper.findAll("button").find((button) => button.text().includes("确认执行清理"))!.trigger("click");
    await flushPromises();
    expect(api.applyStrmCleanupPlan).toHaveBeenCalledWith("plan-1", expect.objectContaining({ expectedRevision: 1, digest: "a".repeat(64), idempotencyKey: expect.any(String) }));
    expect(wrapper.text()).toContain("失效清理已完成，退休 1 个受管 STRM");
    confirm.mockRestore();
  });

  it("previews and confirms reversible empty-directory cleanup", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const plan = {
      plan_id: "empty-plan-1",
      library_id: "main",
      source_scan_run_id: "scan-1",
      source_snapshot_revision: 1,
      plan_hash: "b".repeat(64),
      status: "needs_review" as const,
      revision: 1,
      expires_at: "2026-08-01T00:00:00Z",
      candidate_count: 1,
      executable_count: 1,
      blocked_count: 0,
      candidates: [{ directory_id: "300", parent_id: "200", name: "空目录", path: "受管来源/空目录", state: "ready" as const }],
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      createEmptyDirectoryCleanupPlan: vi.fn().mockResolvedValue(plan),
      applyEmptyDirectoryCleanupPlan: vi.fn().mockResolvedValue({ plan: { ...plan, status: "applied", revision: 3 }, deleted: 1 }),
    };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("预览空目录清理"))!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("受管目录可恢复回收");
    expect(wrapper.text()).toContain("受管来源/空目录");
    await wrapper.findAll("button").find((button) => button.text().includes("确认可恢复回收"))!.trigger("click");
    await flushPromises();
    expect(api.applyEmptyDirectoryCleanupPlan).toHaveBeenCalledWith("empty-plan-1", expect.objectContaining({ expectedRevision: 1, digest: "b".repeat(64), idempotencyKey: expect.any(String) }));
    expect(wrapper.text()).toContain("可恢复回收 1 个受管目录");
    confirm.mockRestore();
  });

  it("polls queued and running STRM operations before showing completion", async () => {
    vi.useFakeTimers();
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const baseOperation = { operation_id: "strm_op_queued", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: null };
    const queued = { ...baseOperation, status: "queued" as const };
    const running = { ...baseOperation, status: "running" as const };
    const succeeded = { ...baseOperation, status: "succeeded" as const, finished_at: "2026-08-01T00:00:01Z" };
    const strmOperation = vi.fn()
      .mockResolvedValueOnce(queued)
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(succeeded);
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      generateStrm: vi.fn().mockResolvedValue({ operation_id: queued.operation_id, library_id: "main", scan_run_id: "scan-1", generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0 }),
      strmOperation,
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await vi.runOnlyPendingTimersAsync();
    await vi.advanceTimersByTimeAsync(0);
    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await vi.advanceTimersByTimeAsync(0);

    expect(wrapper.text()).toContain("已排队");
    expect(wrapper.text()).not.toContain("STRM 全量同步完成");
    await vi.advanceTimersByTimeAsync(500);
    expect(wrapper.text()).toContain("执行中");
    expect(wrapper.text()).not.toContain("STRM 全量同步完成");
    await vi.advanceTimersByTimeAsync(500);
    expect(wrapper.text()).toContain("STRM 全量同步完成");
    expect(strmOperation).toHaveBeenCalledTimes(3);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it.each([
    ["failed", "失败，请查看详情后重试"],
    ["timeout", "超时，请查看详情后重试"],
    ["cancelled", "已取消，可重试"],
  ] as const)("shows a Chinese STRM %s result", async (status, message) => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed", complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const operation = { operation_id: `strm_op_${status}`, library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, status, generated: 0, unchanged: 0, skipped: 0, failed: status === "failed" ? 1 : 0, retired: 0, error_code: `strm_operation_${status}`, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: "2026-08-01T00:00:01Z" };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      generateStrm: vi.fn().mockResolvedValue({ operation_id: operation.operation_id, library_id: "main", scan_run_id: "scan-1", generated: 0, unchanged: 0, skipped: 0, failed: operation.failed, retired: 0 }),
      strmOperation: vi.fn().mockResolvedValue(operation),
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain(message);
    expect(wrapper.text()).toContain("查看详情");
  });

  it("keeps STRM operation history errors visible with retry", async () => {
    const strmOperations = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ items: [], next_cursor: null });
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [{ library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: false, scope_verified: false, revision: 1, latest_scan: null }], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations,
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();
    expect(wrapper.text()).toContain("操作历史加载失败");
    expect(wrapper.text()).toContain("重试");
    await wrapper.findAll("button").find((button) => button.text() === "重试")!.trigger("click");
    await flushPromises();
    expect(strmOperations).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).not.toContain("操作历史加载失败");
  });

  it.each([
    ["strm", "STRM 失效清理未启用，请检查部署功能开关。", "预览失效清理", "overview"],
    ["empty", "空目录回收未就绪，请前往自动整理设置检查开关和写入契约。", "预览空目录清理", "organization"],
  ] as const)("blocks unavailable %s cleanup before a 503 and opens settings", async (_kind, reason, label, section) => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const capability = { enabled: false, reason_code: "disabled", reason_zh: reason as string, settings_section: section };
    const wrapper = mount(LibraryWorkbenchView, {
      props: {
        api: api as never,
        ...(section === "overview" ? { strmCleanupCapability: capability } : { emptyDirectoryCleanupCapability: capability }),
      },
    });
    await flushPromises();

    const cleanupButton = wrapper.findAll("button").find((button) => button.text().includes(label));
    expect(cleanupButton?.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain(reason);
    await wrapper.findAll("button").find((button) => button.text().includes("前往"))!.trigger("click");
    expect(wrapper.emitted("open-settings")?.at(-1)).toEqual([section]);
  });
});
