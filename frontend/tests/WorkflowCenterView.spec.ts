import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../src/api";
import type { WorkflowListResponse, WorkflowResponse } from "../src/types";
import WorkflowCenterView from "../src/views/WorkflowCenterView.vue";

function workflow(overrides: Partial<WorkflowResponse> = {}): WorkflowResponse {
  return {
    id: "workflow-1",
    correlation_id: "correlation-1",
    media_type: "movie",
    tmdb_id: 27205,
    subscription_id: null,
    status: "in_progress",
    status_zh: "处理中",
    state_reason: "stage_in_progress",
    state_reason_zh: "当前阶段处理中。",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:01:00Z",
    stages: [{
      id: "stage-1",
      stage: "discovery",
      status: "running",
      status_zh: "进行中",
      reason: null,
      reason_zh: null,
      error_code: null,
      child_type: null,
      child_id: null,
      started_at: "2026-08-01T00:00:00Z",
      completed_at: null,
      updated_at: "2026-08-01T00:01:00Z",
    }],
    ...overrides,
  };
}

function list(items: WorkflowResponse[]): WorkflowListResponse {
  return { items, page: 1, page_size: 20, total: items.length };
}

function makeApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    workflows: vi.fn().mockResolvedValue(list([workflow()])),
    workflow: vi.fn().mockResolvedValue(workflow()),
    ...overrides,
  } as unknown as ApiClient;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

describe("WorkflowCenterView request ordering", () => {
  it("ignores an older workflow list response after the filters change", async () => {
    const firstResponse = deferred<WorkflowListResponse>();
    const secondResponse = deferred<WorkflowListResponse>();
    const api = makeApi({
      workflows: vi.fn()
        .mockReturnValueOnce(firstResponse.promise)
        .mockReturnValueOnce(secondResponse.promise),
    });
    const wrapper = mount(WorkflowCenterView, { props: { api } });

    await wrapper.get("#workflow-status").setValue("completed");
    secondResponse.resolve(list([workflow({ id: "workflow-new", status: "completed", status_zh: "成功" })]));
    await flushPromises();
    firstResponse.resolve(list([workflow({ id: "workflow-old" })]));
    await flushPromises();

    expect(wrapper.get(".workflow-row strong").text()).toBe("成功");
  });

  it("keeps the newest selected workflow when an older detail response arrives later", async () => {
    const firstDetail = deferred<WorkflowResponse>();
    const secondDetail = deferred<WorkflowResponse>();
    const first = workflow({ id: "workflow-a", status: "in_progress" });
    const second = workflow({ id: "workflow-b", status: "completed", status_zh: "成功" });
    const api = makeApi({
      workflows: vi.fn().mockResolvedValue(list([first, second])),
      workflow: vi.fn()
        .mockReturnValueOnce(firstDetail.promise)
        .mockReturnValueOnce(secondDetail.promise),
    });
    const wrapper = mount(WorkflowCenterView, { props: { api } });
    await flushPromises();

    await wrapper.findAll(".workflow-row")[0].trigger("click");
    await wrapper.findAll(".workflow-row")[1].trigger("click");
    secondDetail.resolve(second);
    await flushPromises();
    firstDetail.resolve(first);
    await flushPromises();

    expect(wrapper.get(".workflow-detail h2").text()).toContain("成功");
  });

  it("keeps the last workflow list visible when a refresh fails", async () => {
    const api = makeApi();
    const workflowsRequest = api.workflows as ReturnType<typeof vi.fn>;
    workflowsRequest
      .mockResolvedValueOnce(list([workflow()]))
      .mockRejectedValueOnce(new ApiError("任务中心暂时无法加载", 503, "workflows_unavailable"));
    const wrapper = mount(WorkflowCenterView, { props: { api } });
    await flushPromises();

    expect(wrapper.findAll(".workflow-row")).toHaveLength(1);
    await wrapper.get(".workflow-toolbar .icon-button").trigger("click");
    await flushPromises();

    expect(wrapper.get(".settings-state-error").text()).toContain("任务中心暂时无法加载");
    expect(wrapper.findAll(".workflow-row")).toHaveLength(1);
    expect(wrapper.text()).toContain("处理中");
  });
});
