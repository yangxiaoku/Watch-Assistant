import { flushPromises, mount } from "@vue/test-utils";
import { useFeedback } from "../src/composables/useFeedback";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api";
import LibraryWorkbenchView from "../src/views/LibraryWorkbenchView.vue";
import type { CapabilityAvailability } from "../src/types";

const enabledCapability: CapabilityAvailability = {
  enabled: true,
  reason_code: null,
  reason_zh: "可执行",
  settings_section: "overview",
};

function mountWorkbench(api: unknown, overrides: Record<string, unknown> = {}) {
  return mount(LibraryWorkbenchView, {
    props: {
      api: api as never,
      organizationPlanCapability: { ...enabledCapability, settings_section: "organization" },
      strmFullCapability: enabledCapability,
      strmIncrementalCapability: enabledCapability,
      strmCleanupCapability: enabledCapability,
      emptyDirectoryCleanupCapability: { ...enabledCapability, settings_section: "organization" },
      ...overrides,
    },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

async function confirmRiskyAction(wrapper: ReturnType<typeof mount>) {
  await wrapper.get(".confirm-dialog-acknowledgement input").setValue(true);
  await wrapper.get(".confirm-dialog-actions button:last-child").trigger("click");
  await flushPromises();
}

async function switchTab(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll(".library-tabs button").find((b) => b.text().includes(label));
  expect(button).toBeDefined();
  await button!.trigger("click");
  await flushPromises();
}

describe("LibraryWorkbenchView", () => {
  beforeEach(() => {
    useFeedback().clear();
  });

  function toastMessages(): string[] {
    return useFeedback().toasts.value.map((item) => item.message);
  }

  it("keeps guarded workbench handlers closed when called outside disabled buttons", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, attempts: 1, state_message_zh: "扫描完成", error_code: null, error_message_zh: null, cancel_requested: false },
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      generateStrm: vi.fn(),
      incrementalStrm: vi.fn(),
      createStrmCleanupPlan: vi.fn(),
      createEmptyDirectoryCleanupPlan: vi.fn(),
      createOrganizationPreview: vi.fn(),
    };
    const unavailable = { enabled: false, reason_code: "disabled", reason_zh: "当前能力不可用。", settings_section: "overview" as const };
    const wrapper = mountWorkbench(api, {
      organizationPlanCapability: { ...unavailable, settings_section: "organization" },
      strmFullCapability: unavailable,
      strmIncrementalCapability: unavailable,
      strmCleanupCapability: unavailable,
      emptyDirectoryCleanupCapability: { ...unavailable, settings_section: "organization" },
    });
    await flushPromises();

    const vm = wrapper.vm as unknown as {
      syncStrm: (action: "full" | "incremental") => Promise<void>;
      previewCleanup: () => Promise<void>;
      previewEmptyDirectoryCleanup: () => Promise<void>;
      createOrganizationPreview: () => Promise<void>;
    };
    await vm.syncStrm("full");
    await vm.syncStrm("incremental");
    await vm.previewCleanup();
    await vm.previewEmptyDirectoryCleanup();
    await vm.createOrganizationPreview();

    expect(api.generateStrm).not.toHaveBeenCalled();
    expect(api.incrementalStrm).not.toHaveBeenCalled();
    expect(api.createStrmCleanupPlan).not.toHaveBeenCalled();
    expect(api.createEmptyDirectoryCleanupPlan).not.toHaveBeenCalled();
    expect(api.createOrganizationPreview).not.toHaveBeenCalled();
  });

  it("keeps directory scans closed until the selected scope is enabled and verified", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: false,
      scope_verified: false,
      revision: 2,
      latest_scan: null,
    };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      scanLibrary: vi.fn(),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();

    const vm = wrapper.vm as unknown as { scanLibrary: () => Promise<void> };
    await vm.scanLibrary();

    expect(api.scanLibrary).not.toHaveBeenCalled();
  });

  it("fails closed when capability status has not been loaded", async () => {
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
    const wrapper = mount(LibraryWorkbenchView, { props: { api: api as never } });
    await flushPromises();

    for (const label of ["生成整理预览", "全量 STRM", "增量同步"]) {
      const button = wrapper.findAll("button").find((item) => item.text().includes(label));
      expect(button?.attributes("disabled")).toBeDefined();
    }
    expect(wrapper.text()).toContain("自动整理计划当前不可用。");
    await switchTab(wrapper, "清理");
    for (const label of ["预览失效清理", "预览空目录清理"]) {
      const button = wrapper.findAll("button").find((item) => item.text().includes(label));
      expect(button?.attributes("disabled")).toBeDefined();
    }
  });

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
    const wrapper = mountWorkbench(api, {
      strmFullCapability: { enabled: false, reason_code: "strm_full_disabled", reason_zh: "STRM 全量生成未启用，请检查部署功能开关。", settings_section: "overview" },
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
    const wrapper = mountWorkbench(api);
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
    const wrapper = mountWorkbench(api);
    await flushPromises();

    const initialize = wrapper.findAll("button").find((button) => button.text().includes("初始化并扫描媒体库"));
    expect(initialize).toBeTruthy();
    await initialize!.trigger("click");
    await flushPromises();

    expect(api.p115Directories).toHaveBeenCalledOnce();
    expect(api.configureLibrary).toHaveBeenCalledWith("main", { name: "115 媒体库", root_directory_id: "123", revision: 0 });
    expect(api.verifyLibraryScope).toHaveBeenCalledWith("main");
    expect(api.scanLibrary).toHaveBeenCalledWith("main");
    expect(toastMessages().some((msg) => msg.includes("现在可以重新推送"))).toBe(true);
  });

  it("continues polling the first scan during initialization", async () => {
    vi.useFakeTimers();
    try {
      const saved = { library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: false, scope_verified: false, revision: 1, latest_scan: null };
      const verified = { ...saved, enabled: true, scope_verified: true, revision: 2 };
      const queued = { run_id: "scan-queued", state: "queued" as const, complete: false, snapshot_revision: null, pages_read: 0, items_seen: 0, added_count: 0, changed_count: 0, removed_count: 0, attempts: 1, state_message_zh: "等待扫描", error_code: null, error_message_zh: null, cancel_requested: false };
      const running = { ...queued, state: "running" as const, state_message_zh: "扫描中", pages_read: 1, items_seen: 1 };
      const completed = { ...running, state: "completed" as const, complete: true, state_message_zh: "扫描完成", snapshot_revision: 2, items_seen: 2, added_count: 2 };
      const api = {
        libraries: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        p115Directories: vi.fn().mockResolvedValue({ parent_id: "123", items: [], has_more: false, next_page: null }),
        configureLibrary: vi.fn().mockResolvedValue(saved),
        verifyLibraryScope: vi.fn().mockResolvedValue({ library: verified, verified: true, enabled: true }),
        scanLibrary: vi.fn().mockResolvedValue(queued),
        getLibraryScan: vi.fn().mockResolvedValueOnce(running).mockResolvedValueOnce(completed),
        libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
        strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      };
      const wrapper = mountWorkbench(api);
      await flushPromises();

      await wrapper.findAll("button").find((button) => button.text().includes("初始化并扫描媒体库"))!.trigger("click");
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1_000);
      await flushPromises();

      expect(api.getLibraryScan).toHaveBeenCalledTimes(2);
      expect(toastMessages().some((msg) => msg.includes("媒体库已启用并完成首次扫描，共发现 2 项"))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("polls an asynchronous scan until the complete snapshot is available", async () => {
    vi.useFakeTimers();
    try {
      const library = {
        library_id: "main",
        name: "115 媒体库",
        root_directory_id: "123",
        enabled: true,
        scope_verified: true,
        revision: 2,
        latest_scan: { run_id: "old-scan", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
      };
      const queued = { run_id: "scan-queued", state: "queued" as const, complete: false, snapshot_revision: null, pages_read: 0, items_seen: 0, added_count: 0, changed_count: 0, removed_count: 0, attempts: 1, state_message_zh: "等待扫描", error_code: null, error_message_zh: null, cancel_requested: false };
      const running = { ...queued, state: "running" as const, state_message_zh: "扫描中", pages_read: 1, items_seen: 1 };
      const completed = { ...running, state: "completed" as const, complete: true, state_message_zh: "扫描完成", snapshot_revision: 2, items_seen: 2, added_count: 1 };
      const api = {
        libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
        scanLibrary: vi.fn().mockResolvedValue(queued),
        getLibraryScan: vi.fn().mockResolvedValueOnce(running).mockResolvedValueOnce(completed),
        libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
        strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      };
      const wrapper = mountWorkbench(api);
      await flushPromises();

      await wrapper.findAll("button").find((button) => button.text().includes("扫描目录"))!.trigger("click");
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1_000);
      await flushPromises();

      expect(api.getLibraryScan).toHaveBeenCalledTimes(2);
      expect(toastMessages().some((msg) => msg.includes("扫描完成，共发现 2 项"))).toBe(true);
      expect(wrapper.text()).toContain("已完成");
    } finally {
      vi.useRealTimers();
    }
  });

  it("resumes a running scan after the workbench is loaded", async () => {
    vi.useFakeTimers();
    try {
      const running = { run_id: "scan-running", state: "running" as const, complete: false, snapshot_revision: null, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, attempts: 1, state_message_zh: "扫描中", error_code: null, error_message_zh: null, cancel_requested: false };
      const completed = { ...running, state: "completed" as const, complete: true, snapshot_revision: 2, state_message_zh: "扫描完成", items_seen: 2 };
      const library = { library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: true, scope_verified: true, revision: 2, latest_scan: running };
      const api = {
        libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
        getLibraryScan: vi.fn().mockResolvedValue(completed),
        libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
        strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      };
      const wrapper = mountWorkbench(api);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(500);
      await flushPromises();

      expect(api.getLibraryScan).toHaveBeenCalledWith("main", "scan-running");
      expect(wrapper.get(".library-stat-grid").text()).toContain("扫描状态已完成");
      expect(wrapper.get(".library-stat-grid").text()).toContain("文件数2");
    } finally {
      vi.useRealTimers();
    }
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
    const wrapper = mountWorkbench(api);
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await flushPromises();
    expect(api.generateStrm).toHaveBeenCalledWith("main", "scan-1");
    expect(toastMessages().some((msg) => msg.includes("STRM 全量同步完成"))).toBe(true);


    await switchTab(wrapper, "清理");

    await wrapper.findAll("button").find((button) => button.text().includes("预览失效清理"))!.trigger("click");
    await flushPromises();
    expect(toastMessages().some((msg) => msg.includes("失效清理预览已生成"))).toBe(true);

    await wrapper.findAll("button").find((button) => button.text().includes("查看摘要并确认清理"))!.trigger("click");
    expect(wrapper.get(".confirm-dialog").text()).toContain("可退休");
    await confirmRiskyAction(wrapper);
    expect(api.applyStrmCleanupPlan).toHaveBeenCalledWith("plan-1", expect.objectContaining({ expectedRevision: 1, digest: "a".repeat(64), idempotencyKey: expect.any(String) }));
    expect(toastMessages().some((msg) => msg.includes("失效清理已完成，退休 1 个受管 STRM"))).toBe(true);
  });

  it("loads later STRM manifest pages and presents item states in Chinese", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const manifestPage = (page: number, name: string) => ({
      items: [{ manifest_id: `manifest-${page}`, library_id: "main", cloud_file_id: `cloud-${page}`, cloud_relative_path: name, local_relative_path: `${name}.strm`, status: "verified" as const, source_version: page }],
      page,
      page_size: 1,
      total: 2,
      total_pages: 2,
    });
    const strmManifest = vi.fn()
      .mockResolvedValueOnce(manifestPage(1, "第一项"))
      .mockResolvedValueOnce(manifestPage(2, "第二项"));
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest,
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();
    await switchTab(wrapper, "文件");

    expect(wrapper.text()).toContain("第一项");
    expect(wrapper.text()).toContain("有效");
    await wrapper.get('button[aria-label="下一页"]').trigger("click");
    await flushPromises();

    expect(strmManifest).toHaveBeenNthCalledWith(2, "main", 2, 1);
    expect(wrapper.text()).toContain("第二项");
  });

  it("appends media and STRM operation cursor pages", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 2, added_count: 2, changed_count: 0, removed_count: 0, error_code: null },
    };
    const firstMedia = { media_id: "media-1", library_id: "main", scan_run_id: "scan-1", object_type: "file" as const, object_id: "object-1", parent_id: null, name: "第一文件.mkv", size_bytes: 1, modified_at: null, state: "indexed" as const };
    const secondMedia = { ...firstMedia, media_id: "media-2", object_id: "object-2", name: "第二文件.mkv" };
    const firstOperation = { operation_id: "operation-1", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, status: "succeeded" as const, generated: 1, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: null, finished_at: null };
    const secondOperation = { ...firstOperation, operation_id: "operation-2", created_at: "2026-08-02T00:00:00Z" };
    const libraryMedia = vi.fn()
      .mockResolvedValueOnce({ items: [firstMedia], next_cursor: 101 })
      .mockResolvedValueOnce({ items: [secondMedia], next_cursor: null });
    const strmOperations = vi.fn()
      .mockResolvedValueOnce({ items: [firstOperation], next_cursor: "operation-cursor" })
      .mockResolvedValueOnce({ items: [secondOperation], next_cursor: null });
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia,
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations,
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();
    await switchTab(wrapper, "文件");

    await wrapper.findAll("button").find((button) => button.text() === "加载更多索引文件")!.trigger("click");
    await wrapper.findAll("button").find((button) => button.text() === "加载更多操作")!.trigger("click");
    await flushPromises();

    expect(libraryMedia).toHaveBeenNthCalledWith(2, "main", 101);
    expect(strmOperations).toHaveBeenNthCalledWith(2, "main", "operation-cursor");
    expect(wrapper.text()).toContain("第一文件.mkv");
    expect(wrapper.text()).toContain("第二文件.mkv");
    expect(wrapper.text()).toContain("2 条");
  });

  it("paginates media library choices while keeping the current selection", async () => {
    const firstLibrary = { library_id: "first", name: "第一媒体库", root_directory_id: "1", enabled: true, scope_verified: true, revision: 1, latest_scan: null };
    const secondLibrary = { library_id: "second", name: "第二媒体库", root_directory_id: "2", enabled: true, scope_verified: true, revision: 1, latest_scan: null };
    const libraries = vi.fn()
      .mockResolvedValueOnce({ items: [firstLibrary], next_cursor: 50 })
      .mockResolvedValueOnce({ items: [secondLibrary], next_cursor: null })
      .mockResolvedValueOnce({ items: [firstLibrary], next_cursor: 50 })
      .mockResolvedValueOnce({ items: [secondLibrary], next_cursor: null });
    const api = {
      libraries,
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();

    expect(wrapper.get(".library-detail-heading h2").text()).toBe("第一媒体库");
    await wrapper.get("button.library-load-more").trigger("click");
    await flushPromises();

    expect(libraries).toHaveBeenNthCalledWith(2, 50);
    expect(wrapper.findAll(".library-scope-row")).toHaveLength(2);
    expect(wrapper.get(".library-detail-heading h2").text()).toBe("第一媒体库");
    await wrapper.findAll(".library-scope-row")[1].trigger("click");
    await flushPromises();
    expect(wrapper.get(".library-detail-heading h2").text()).toBe("第二媒体库");
    expect(wrapper.find("button.library-load-more").exists()).toBe(false);

    await wrapper.get('button[aria-label="刷新媒体库"]').trigger("click");
    await flushPromises();
    expect(libraries).toHaveBeenNthCalledWith(3);
    expect(libraries).toHaveBeenNthCalledWith(4, 50);
    expect(wrapper.get(".library-detail-heading h2").text()).toBe("第二媒体库");
  });

  it("drops stale output responses after switching libraries", async () => {
    const firstMedia = deferred<{ items: Array<Record<string, unknown>>; next_cursor: number | null }>();
    const secondMedia = deferred<{ items: Array<Record<string, unknown>>; next_cursor: number | null }>();
    const firstLibrary = { library_id: "first", name: "第一媒体库", root_directory_id: "1", enabled: true, scope_verified: true, revision: 1, latest_scan: null };
    const secondLibrary = { library_id: "second", name: "第二媒体库", root_directory_id: "2", enabled: true, scope_verified: true, revision: 1, latest_scan: null };
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [firstLibrary, secondLibrary], next_cursor: null }),
      libraryMedia: vi.fn().mockReturnValueOnce(firstMedia.promise).mockReturnValueOnce(secondMedia.promise),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();
    await switchTab(wrapper, "文件");

    await wrapper.findAll(".library-scope-row")[1].trigger("click");
    secondMedia.resolve({ items: [{ media_id: "second-media", library_id: "second", scan_run_id: "scan-2", object_type: "file", object_id: "object-2", parent_id: null, name: "第二媒体文件.mkv", size_bytes: null, modified_at: null, state: "indexed" }], next_cursor: null });
    await flushPromises();
    firstMedia.resolve({ items: [{ media_id: "first-media", library_id: "first", scan_run_id: "scan-1", object_type: "file", object_id: "object-1", parent_id: null, name: "第一媒体文件.mkv", size_bytes: null, modified_at: null, state: "indexed" }], next_cursor: null });
    await flushPromises();

    expect(wrapper.text()).toContain("第二媒体文件.mkv");
    expect(wrapper.text()).not.toContain("第一媒体文件.mkv");
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
    const wrapper = mountWorkbench(api);
    await flushPromises();


    await switchTab(wrapper, "清理");

    await wrapper.findAll("button").find((button) => button.text().includes("预览空目录清理"))!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("受管目录可恢复回收");
    expect(wrapper.text()).toContain("受管来源/空目录");
    await wrapper.findAll("button").find((button) => button.text().includes("查看摘要并确认回收"))!.trigger("click");
    expect(wrapper.get(".confirm-dialog").text()).toContain("可恢复回收");
    await confirmRiskyAction(wrapper);
    expect(api.applyEmptyDirectoryCleanupPlan).toHaveBeenCalledWith("empty-plan-1", expect.objectContaining({ expectedRevision: 1, digest: "b".repeat(64), idempotencyKey: expect.any(String) }));
    expect(toastMessages().some((msg) => msg.includes("可恢复回收 1 个受管目录"))).toBe(true);
  });

  it("applies small-file cleanup asynchronously and polls the operation to completion", async () => {
    vi.useFakeTimers();
    try {
      const library = {
        library_id: "main",
        name: "115 媒体库",
        root_directory_id: "123",
        enabled: true,
        scope_verified: true,
        revision: 2,
        latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
      };
      const preview = {
        library_id: "main",
        source_scan_run_id: "scan-1",
        snapshot_revision: 1,
        threshold_bytes: 5 * 1024 * 1024,
        candidate_count: 1,
        candidates: [{ file_id: "file-1", parent_id: "100", name: "sample.srt", size_bytes: 1024 }],
      };
      const running = { operation_id: "strm_op_small", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "small_file_cleanup" as const, status: "running" as const, generated: 0, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: null };
      const succeeded = { ...running, status: "succeeded" as const, retired: 1, failed: 0, finished_at: "2026-08-01T00:00:01Z" };
      const api = {
        libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
        libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
        strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        smallFileCleanupPreview: vi.fn().mockResolvedValue(preview),
        smallFileCleanupApply: vi.fn().mockResolvedValue({ operation_id: "strm_op_small", status: "running" }),
        strmOperation: vi.fn()
          .mockResolvedValueOnce(running)
          .mockResolvedValueOnce(succeeded),
      };
      const wrapper = mountWorkbench(api);
      await vi.runOnlyPendingTimersAsync();
      await vi.advanceTimersByTimeAsync(0);
      await switchTab(wrapper, "清理");

      await wrapper.findAll("button").find((button) => button.text().includes("预览小文件清理"))!.trigger("click");
      await flushPromises();
      expect(wrapper.text()).toContain("sample.srt");
      expect(toastMessages().some((msg) => msg.includes("小文件清理预览已生成"))).toBe(true);

      await wrapper.findAll("button").find((button) => button.text().includes("查看摘要并确认清理"))!.trigger("click");
      expect(wrapper.get(".confirm-dialog").text()).toContain("确认清理小文件");
      await confirmRiskyAction(wrapper);

      expect(api.smallFileCleanupApply).toHaveBeenCalledWith("main", expect.objectContaining({ sourceScanRunId: "scan-1", fileIds: ["file-1"], confirm: true }));
      expect(toastMessages().some((msg) => msg.includes("小文件清理执行中"))).toBe(true);
      expect(toastMessages().some((msg) => msg.includes("小文件清理完成"))).toBe(false);

      await vi.advanceTimersByTimeAsync(500);
      await flushPromises();
      expect(toastMessages().some((msg) => msg.includes("小文件清理完成：删除 1 项"))).toBe(true);
      expect(api.strmOperation).toHaveBeenCalledTimes(2);
      wrapper.unmount();
    } finally {
      vi.useRealTimers();
    }
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
    const wrapper = mountWorkbench(api);
    await vi.runOnlyPendingTimersAsync();
    await vi.advanceTimersByTimeAsync(0);
    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await vi.advanceTimersByTimeAsync(0);

    expect(toastMessages().some((msg) => msg.includes("已排队"))).toBe(true);
    expect(toastMessages().some((msg) => msg.includes("STRM 全量同步完成"))).toBe(false);
    await vi.advanceTimersByTimeAsync(500);
    expect(toastMessages().some((msg) => msg.includes("执行中"))).toBe(true);
    expect(toastMessages().some((msg) => msg.includes("STRM 全量同步完成"))).toBe(false);
    await vi.advanceTimersByTimeAsync(500);
    expect(toastMessages().some((msg) => msg.includes("STRM 全量同步完成"))).toBe(true);
    expect(strmOperation).toHaveBeenCalledTimes(3);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("keeps a polling timeout as a pending result instead of an error", async () => {
    vi.useFakeTimers();
    try {
      const library = {
        library_id: "main",
        name: "115 媒体库",
        root_directory_id: "123",
        enabled: true,
        scope_verified: true,
        revision: 2,
        latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, attempts: 1, state_message_zh: "扫描完成", error_code: null, error_message_zh: null, cancel_requested: false },
      };
      const running = { operation_id: "strm_op_running", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, status: "running" as const, generated: 0, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: null };
      const api = {
        libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
        libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
        strmOperations: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
        generateStrm: vi.fn().mockResolvedValue({ operation_id: running.operation_id, library_id: "main", scan_run_id: "scan-1", generated: 0, unchanged: 0, skipped: 0, failed: 0, retired: 0 }),
        strmOperation: vi.fn().mockResolvedValue(running),
      };
      const wrapper = mountWorkbench(api);
      await vi.runOnlyPendingTimersAsync();
      await vi.advanceTimersByTimeAsync(0);
      await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
      await vi.advanceTimersByTimeAsync(60_000);
      await flushPromises();

      expect(toastMessages().some((msg) => msg.includes("页面已停止自动等待"))).toBe(true);
      expect(wrapper.find(".inline-alert").exists()).toBe(false);
      expect(toastMessages().some((msg) => msg.includes("确认前不要恢复执行"))).toBe(true);
      expect(api.strmOperation).toHaveBeenCalledTimes(120);
      wrapper.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["failed", "失败，请查看详情后重试"],
    ["timeout", "结果待确认，请先刷新并核对结果，确认前不要恢复执行"],
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
    const wrapper = mountWorkbench(api);
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text().includes("全量 STRM"))!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain(message);
    expect(wrapper.text()).toContain("查看详情");
  });

  it("resumes the persisted STRM operation shown after reload", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const failed = { operation_id: "strm_op_failed", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "full" as const, status: "failed" as const, generated: 0, unchanged: 0, skipped: 0, failed: 1, retired: 0, error_code: "strm_operation_failed", created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: "2026-08-01T00:00:01Z" };
    const succeeded = { ...failed, status: "succeeded" as const, failed: 0, generated: 1, error_code: null };
    const strmOperations = vi.fn()
      .mockResolvedValueOnce({ items: [failed], next_cursor: null })
      .mockResolvedValueOnce({ items: [succeeded], next_cursor: null });
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations,
      resumeStrmOperation: vi.fn().mockResolvedValue(succeeded),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();

    expect(wrapper.text()).toContain("恢复执行");
    await wrapper.findAll("button").find((button) => button.text().includes("恢复执行"))!.trigger("click");
    await flushPromises();

    expect(api.resumeStrmOperation).toHaveBeenCalledWith("strm_op_failed");
    expect(toastMessages().some((msg) => msg.includes("STRM 全量同步完成"))).toBe(true);
  });

  it("cancels a running STRM operation from the workbench", async () => {
    const library = {
      library_id: "main",
      name: "115 媒体库",
      root_directory_id: "123",
      enabled: true,
      scope_verified: true,
      revision: 2,
      latest_scan: { run_id: "scan-1", state: "completed" as const, complete: true, snapshot_revision: 1, pages_read: 1, items_seen: 1, added_count: 1, changed_count: 0, removed_count: 0, error_code: null },
    };
    const running = { operation_id: "strm_op_running", library_id: "main", source_scan_run_id: "scan-1", workflow_id: null, kind: "incremental" as const, status: "running" as const, generated: 0, unchanged: 0, skipped: 0, failed: 0, retired: 0, error_code: null, created_at: "2026-08-01T00:00:00Z", started_at: "2026-08-01T00:00:00Z", finished_at: null };
    const cancelled = { ...running, status: "cancelled" as const, error_code: "strm_operation_cancelled", finished_at: "2026-08-01T00:00:01Z" };
    const strmOperations = vi.fn()
      .mockResolvedValueOnce({ items: [running], next_cursor: null })
      .mockResolvedValueOnce({ items: [cancelled], next_cursor: null });
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [library], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations,
      cancelStrmOperation: vi.fn().mockResolvedValue(cancelled),
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("取消操作"))!.trigger("click");
    expect(api.cancelStrmOperation).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").text()).toContain("确认取消 STRM 操作");
    expect(wrapper.find(".confirm-dialog-acknowledgement").exists()).toBe(false);
    await wrapper.get(".confirm-dialog-actions button:last-child").trigger("click");
    await flushPromises();

    expect(api.cancelStrmOperation).toHaveBeenCalledWith("strm_op_running");
    expect(toastMessages().some((msg) => msg.includes("STRM 增量同步已取消"))).toBe(true);
  });

  it("keeps STRM operation history errors visible with retry", async () => {
    const strmOperations = vi.fn()
      .mockRejectedValueOnce(new ApiError("STRM 操作历史服务暂不可用，请稍后重试", 503, "operation_unavailable"))
      .mockResolvedValueOnce({ items: [], next_cursor: null });
    const api = {
      libraries: vi.fn().mockResolvedValue({ items: [{ library_id: "main", name: "115 媒体库", root_directory_id: "123", enabled: false, scope_verified: false, revision: 1, latest_scan: null }], next_cursor: null }),
      libraryMedia: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      strmManifest: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 }),
      strmOperations,
    };
    const wrapper = mountWorkbench(api);
    await flushPromises();
    await switchTab(wrapper, "文件");
    expect(wrapper.text()).toContain("STRM 操作历史服务暂不可用，请稍后重试");
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
    const wrapper = mountWorkbench(api, section === "overview" ? { strmCleanupCapability: capability } : { emptyDirectoryCleanupCapability: capability });
    await flushPromises();

    expect(wrapper.text()).toContain(reason);
    await wrapper.findAll("button").find((button) => button.text().includes("前往"))!.trigger("click");
    expect(wrapper.emitted("open-settings")?.at(-1)).toEqual([section]);
    await switchTab(wrapper, "清理");
    const cleanupButton = wrapper.findAll("button").find((button) => button.text().includes(label));
    expect(cleanupButton?.attributes("disabled")).toBeDefined();
  });
});
