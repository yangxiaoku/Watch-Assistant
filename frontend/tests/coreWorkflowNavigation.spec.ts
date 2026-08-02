import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import TaskDrawer from "../src/components/TaskDrawer.vue";
import WorkflowCenterView from "../src/views/WorkflowCenterView.vue";

describe("core workflow navigation", () => {
  it("names the drawer as push tasks and links it to the workflow center", async () => {
    const wrapper = mount(TaskDrawer, { props: { tasks: [], open: true } });

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
});
