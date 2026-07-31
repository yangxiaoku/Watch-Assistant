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

  it("shows the durable STRM operation after synchronization", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: {
        run_id: "scan-1",
        state: "completed",
        complete: true,
        snapshot_revision: 1,
        pages_read: 1,
        items_seen: 2,
        added_count: 2,
        changed_count: 0,
        removed_count: 0,
        error_code: null,
      },
    };
    const generated = { operation_id: "strm_op_1", library_id: "main", scan_run_id: "scan-1", generated: 2, unchanged: 1, skipped: 0, failed: 0, retired: 0 };
    const operation = {
      ...generated,
      workflow_id: null,
      kind: "full",
      status: "succeeded",
      error_code: null,
      created_at: "2026-07-31T00:00:00Z",
      started_at: "2026-07-31T00:00:00Z",
      finished_at: "2026-07-31T00:00:01Z",
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      generateStrm: vi.fn().mockResolvedValue(generated),
      strmOperation: vi.fn().mockResolvedValue(operation),
      createOrganizationPreview: vi.fn(),
      incrementalStrm: vi.fn(),
      cleanupStrm: vi.fn(),
    };
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    const full = wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"));
    expect(full).toBeTruthy();
    await full!.trigger("click");
    await flushPromises();

    expect(api.strmOperation).toHaveBeenCalledWith("strm_op_1");
    expect(wrapper.text()).toContain("持久操作账本");
    expect(wrapper.text()).toContain("全量生成");
    expect(wrapper.text()).toContain("已完成");
    expect(wrapper.text()).toContain("生成 2");
  });
});
