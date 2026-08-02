import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import TaskDrawer from "../src/components/TaskDrawer.vue";
import WorkflowCenterView from "../src/views/WorkflowCenterView.vue";

describe("core workflow navigation", () => {
  const api = {
    reconcileTask: vi.fn(),
  };

  it("names the drawer as push tasks and links it to the workflow center", async () => {
    const wrapper = mount(TaskDrawer, { props: { api: api as never, tasks: [], open: true } });

    expect(wrapper.get("aside").attributes("aria-label")).toBe("推送任务");
    expect(wrapper.get("h2").text()).toBe("推送任务");
    expect(wrapper.text()).not.toContain("推送记录");

    await wrapper.get(".task-drawer-header-actions .text-button").trigger("click");
    expect(wrapper.emitted("navigate")).toEqual([["workflows"]]);
  });

  it("lets the workflow center open the push-task drawer", async () => {
    const api = {
      workflows: vi.fn().mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 }),
    };
    const wrapper = mount(WorkflowCenterView, { props: { api: api as never } });
    await flushPromises();

    await wrapper.get(".workflow-push-link").trigger("click");
    expect(wrapper.emitted("open-push-tasks")).toHaveLength(1);
  });

  it("shows submitted states separately and advances only from read-only evidence", async () => {
    const submitted = {
      id: "task-submitted",
      resource_id: "resource-1",
      workflow_id: "workflow-1",
      target_directory_id: null,
      action: "offline_download" as const,
      state: "submitted" as const,
      attempts: 1,
      remote_ref: "remote-1",
      error_code: null,
      error_message: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
      submitted_at: "2026-08-01T00:00:01Z",
      state_zh: "已受理，等待文件可用",
      state_reason_zh: "115 已受理，尚未取得文件可用证据。",
    };
    const downloading = { ...submitted, id: "task-downloading", state: "downloading" as const, state_zh: "下载中，等待文件可用" };
    const available = { ...submitted, id: "task-available", state: "available" as const, state_zh: "文件已可用", state_reason_zh: "已取得只读文件可用证据。" };
    const reconcileTask = vi.fn().mockResolvedValue({
      task: { ...submitted, state: "available", state_zh: "文件已可用" },
      evidence: {
        id: "evidence-1",
        workflow_id: "workflow-1",
        task_id: "task-submitted",
        stage: "availability",
        evidence_type: "remote_status",
        source: "readonly_reconciliation",
        subject_id: "remote-1",
        status: "available",
        verified: true,
        observed_at: "2026-08-01T00:00:02Z",
      },
    });
    const wrapper = mount(TaskDrawer, {
      props: { api: { reconcileTask } as never, tasks: [submitted, downloading, available], open: true },
    });

    expect(wrapper.text()).toContain("已受理，等待文件可用");
    expect(wrapper.text()).toContain("下载中，等待文件可用");
    expect(wrapper.text()).toContain("文件已可用");
    expect(wrapper.findAll(".task-action")).toHaveLength(2);

    await wrapper.findAll(".task-action")[0].trigger("click");
    await flushPromises();

    expect(reconcileTask).toHaveBeenCalledWith("task-submitted");
    expect(wrapper.emitted("updated")?.[0]?.[0]).toMatchObject({ id: "task-submitted", state: "available" });
  });
});
