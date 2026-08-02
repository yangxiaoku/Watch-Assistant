import { expect, test } from "@playwright/test";

const workflow = {
  id: "workflow-private-360",
  correlation_id: "correlation-private-360",
  media_type: "movie" as const,
  tmdb_id: 27205,
  subscription_id: null,
  status: "result_pending_confirmation" as const,
  status_zh: "结果待确认",
  state_reason: "uncertain_requires_verification",
  state_reason_zh: "先核对远端结果，确认前不要重复操作。",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:01:00Z",
  stages: [{
    id: "stage-private-360",
    stage: "push" as const,
    status: "uncertain" as const,
    status_zh: "结果待确认",
    reason: "uncertain_requires_verification",
    reason_zh: "远端结果需要只读核对。",
    error_code: "uncertain_requires_verification",
    child_type: "task",
    child_id: "task-private-360",
    started_at: "2026-08-01T00:00:00Z",
    completed_at: null,
    updated_at: "2026-08-01T00:01:00Z",
  }],
};

const task = {
  id: "task-private-360",
  resource_id: "resource-private-360",
  workflow_id: workflow.id,
  target_directory_id: null,
  action: "offline_download" as const,
  state: "failed" as const,
  attempts: 1,
  remote_ref: null,
  error_code: "internal_error",
  error_message: "raw backend exception must stay hidden",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:01:00Z",
  submitted_at: null,
  state_zh: "处理失败",
  state_reason_zh: null,
};

test("keeps task and workflow workbench usable at 360px", async ({ page }) => {
  let taskState: "failed" | "queued" | "cancelled" = task.state;
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", push_supported: false, inspection_supported: false } }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));
  await page.route("**/api/v1/workflows?**", (route) => route.fulfill({ json: { items: [workflow], page: 1, page_size: 20, total: 1 } }));
  await page.route(`**/api/v1/workflows/${workflow.id}`, (route) => route.fulfill({ json: workflow }));
  await page.route("**/api/v1/tasks", (route) => route.fulfill({ json: [{ ...task, state: taskState }] }));
  await page.route(`**/api/v1/tasks/${task.id}/retry`, (route) => {
    taskState = "queued";
    return route.fulfill({ json: { ...task, state: taskState, state_zh: "排队中", error_code: null, error_message: null } });
  });
  await page.route(`**/api/v1/tasks/${task.id}/cancel`, (route) => {
    taskState = "cancelled";
    return route.fulfill({ json: { ...task, state: taskState, state_zh: "已取消", error_code: null, error_message: null } });
  });
  await page.route(`**/api/v1/tasks/${task.id}`, (route) => route.fulfill({ json: { ...task, state: taskState, state_zh: taskState === "queued" ? "排队中" : task.state_zh } }));

  await page.goto("/workflows");
  await expect(page.getByRole("heading", { name: "任务中心" })).toBeVisible();
  await page.locator(".workflow-row").first().click();
  await expect(page.locator(".workflow-detail").getByText("结果待确认").first()).toBeVisible();
  await expect(page.getByText(workflow.id, { exact: true })).toBeHidden();
  await expect(page.locator(".workflow-diagnostics")).toHaveCount(1);

  await page.getByRole("button", { name: "推送任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "推送任务" })).toBeVisible();
  await expect(page.getByText("raw backend exception must stay hidden", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "重新排队" }).click();
  await expect(page.getByRole("button", { name: "取消排队" })).toBeVisible();
  await page.getByRole("button", { name: "取消排队" }).click();
  await page.getByRole("dialog").getByRole("checkbox", { name: "我已核对上述摘要，确认继续此操作" }).check();
  await page.getByRole("dialog").getByRole("button", { name: "确认取消" }).click();
  await expect(page.locator(".task-drawer").getByText("已取消").first()).toBeVisible();

  const layout = await page.evaluate(() => {
    const visible = Array.from(document.querySelectorAll<HTMLElement>("body *")).filter((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    });
    const overflow = visible.filter((element) => {
      if (element.closest(".primary-nav")) return false;
      const rect = element.getBoundingClientRect();
      return rect.left < -1 || rect.right > window.innerWidth + 1;
    }).slice(0, 5).map((element) => element.className || element.tagName);
    const title = document.querySelector<HTMLElement>(".task-drawer h2")?.getBoundingClientRect();
    const actions = document.querySelector<HTMLElement>(".task-drawer-header-actions")?.getBoundingClientRect();
    return {
      width: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      overflow,
      headerOverlaps: Boolean(title && actions && title.right > actions.left),
    };
  });
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.width);
  expect(layout.overflow).toEqual([]);
  expect(layout.headerOverlaps).toBe(false);
});
