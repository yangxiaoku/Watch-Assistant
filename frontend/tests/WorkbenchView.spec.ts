import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import WorkbenchView from "../src/views/WorkbenchView.vue";

const available = (reason = "可执行") => ({ enabled: true, reason_code: null, reason_zh: reason, settings_section: "overview" as const });
const disabled = (reasonCode = "organization_plan_disabled", reason = "功能未启用，请前往设置。") => ({ enabled: false, reason_code: reasonCode, reason_zh: reason, settings_section: "overview" as const });

describe("WorkbenchView", () => {
  it("provides a searchable Chinese workflow entry point", async () => {
    const wrapper = mount(WorkbenchView, {
      props: {
        organizationPlanCapability: available(),
        strmFullCapability: available(),
        strmIncrementalCapability: disabled("strm_incremental_disabled"),
      },
    });

    expect(wrapper.get("h1").text()).toBe("观影工作台");
    expect(wrapper.findAll(".workbench-entry")).toHaveLength(5);
    await wrapper.get("#workbench-search-input").setValue("沙丘");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("search")).toEqual([["沙丘"]]);
  });

  it("routes unavailable organizing and STRM capabilities to settings", async () => {
    const wrapper = mount(WorkbenchView, {
      props: {
        organizationPlanCapability: disabled(),
        strmFullCapability: disabled("strm_full_disabled"),
        strmIncrementalCapability: disabled("strm_incremental_disabled"),
      },
    });

    expect(wrapper.text()).toContain("需配置");
    const actions = wrapper.findAll(".workbench-entry-action");
    await actions[2].trigger("click");
    await actions[3].trigger("click");
    expect(wrapper.emitted("navigate")).toEqual([["settings"], ["settings"]]);
    expect(wrapper.text()).toContain("全量和增量 STRM 均未启用");
  });

  it("does not submit an empty search", async () => {
    const wrapper = mount(WorkbenchView, {
      props: {
        organizationPlanCapability: available(),
        strmFullCapability: available(),
        strmIncrementalCapability: available(),
      },
    });

    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("search")).toBeUndefined();
    expect(wrapper.text()).toContain("请输入影片名或剧名");
  });
});
