import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import LibraryWorkbenchView from "../src/views/LibraryWorkbenchView.vue";

describe("LibraryWorkbenchView", () => {
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
});
