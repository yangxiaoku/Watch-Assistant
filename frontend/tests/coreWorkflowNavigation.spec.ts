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

  it("keeps cancellation available for pending stages beside running work", async () => {
    const workflow = {
      id: "workflow-mixed",
      correlation_id: "correlation-mixed",
      media_type: "movie" as const,
      tmdb_id: 27205,
      subscription_id: null,
      status: "in_progress" as const,
      status_zh: "处理中",
      state_reason: "stage_in_progress",
      state_reason_zh: "当前阶段处理中。",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:01:00Z",
      stages: [
        {
          id: "stage-running",
          stage: "push" as const,
          status: "running" as const,
          status_zh: "处理中",
          reason: null,
          reason_zh: null,
          error_code: null,
          child_type: "task",
          child_id: "task-running",
          started_at: "2026-08-01T00:00:00Z",
          completed_at: null,
          updated_at: "2026-08-01T00:01:00Z",
        },
        {
          id: "stage-pending",
          stage: "availability" as const,
          status: "pending" as const,
          status_zh: "待开始",
          reason: null,
          reason_zh: null,
          error_code: null,
          child_type: null,
          child_id: null,
          started_at: null,
          completed_at: null,
          updated_at: "2026-08-01T00:01:00Z",
        },
      ],
    };
    const cancelWorkflow = vi.fn().mockResolvedValue({ ...workflow, status: "partial", status_zh: "部分完成" });
    const api = {
      workflows: vi.fn().mockResolvedValue({ items: [workflow], page: 1, page_size: 20, total: 1 }),
      workflow: vi.fn().mockResolvedValue(workflow),
      cancelWorkflow,
    };
    const wrapper = mount(WorkflowCenterView, { props: { api: api as never } });
    await flushPromises();

    await wrapper.get(".workflow-row").trigger("click");
    await flushPromises();
    expect(wrapper.get(".workflow-actions").text()).toContain("取消工作流");
    await wrapper.get(".workflow-actions").get("button:last-child").trigger("click");
    expect(wrapper.text()).toContain("运行中或结果待确认的远端操作不会被伪造撤回");

    await wrapper.get(".confirm-dialog-acknowledgement input").setValue(true);
    await wrapper.get(".confirm-dialog-actions button:last-child").trigger("click");
    await flushPromises();
    expect(cancelWorkflow).toHaveBeenCalledWith("workflow-mixed");
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

  it("only retries terminal failures and confirms queued cancellation", async () => {
    const base = {
      resource_id: "resource-1",
      workflow_id: null,
      target_directory_id: null,
      action: "offline_download" as const,
      attempts: 1,
      remote_ref: null,
      error_code: null,
      error_message: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
      submitted_at: null,
      state_reason_zh: null,
    };
    const failed = { ...base, id: "task-failed", state: "failed" as const, state_zh: "处理失败", error_code: "internal_error" };
    const queued = { ...base, id: "task-queued", state: "queued" as const, state_zh: "排队中" };
    const uncertain = { ...base, id: "task-uncertain", state: "uncertain" as const, state_zh: "结果待确认", remote_ref: "remote-1", state_reason_zh: "请先核对，不要重复提交。" };
    const retryTask = vi.fn().mockResolvedValue({ ...failed, state: "queued", state_zh: "排队中", error_code: null });
    const cancelTask = vi.fn().mockResolvedValue({ ...queued, state: "cancelled", state_zh: "已取消" });
    const reconcileTask = vi.fn().mockResolvedValue({ task: { ...uncertain, state: "available", state_zh: "文件已可用" }, evidence: {} });
    const wrapper = mount(TaskDrawer, {
      props: { api: { retryTask, cancelTask, reconcileTask } as never, tasks: [failed, queued, uncertain], open: true },
    });

    expect(wrapper.findAll("button").some((button) => button.text().includes("重新排队"))).toBe(true);
    expect(wrapper.findAll("button").some((button) => button.text().includes("取消排队"))).toBe(true);
    expect(wrapper.findAll("button").some((button) => button.text().includes("重提结果待确认"))).toBe(false);

    await wrapper.findAll("button").find((button) => button.text().includes("重新排队"))!.trigger("click");
    await flushPromises();
    expect(retryTask).toHaveBeenCalledWith("task-failed");

    const cancelButtons = wrapper.findAll("button").filter((button) => button.text().includes("取消排队"));
    await cancelButtons.at(-1)!.trigger("click");
    expect(cancelTask).not.toHaveBeenCalled();
    await wrapper.get(".confirm-dialog-acknowledgement input").setValue(true);
    await wrapper.get(".confirm-dialog-actions button:last-child").trigger("click");
    await flushPromises();
    expect(cancelTask).toHaveBeenCalledWith("task-queued");
    expect(wrapper.text()).toContain("结果待确认");
  });
});
