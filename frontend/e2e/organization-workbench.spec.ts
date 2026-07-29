import { expect, test } from "@playwright/test";

const needsReview = {
  plan_id: "plan-local-1",
  plan_hash: "a".repeat(64),
  status: "needs_review",
  revision: 4,
  expires_at: "2026-08-01T00:00:00Z",
  source_count: 2,
  action_count: 1,
  precondition_count: 2,
  alias: null,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, organization_plan_enabled: true } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-local" } }),
  );
  await page.route("**/api/v1/organization-plans**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      await route.fulfill({ json: { ...needsReview, status: "planned", revision: 5 } });
      return;
    }
    await route.fulfill({ json: { items: [needsReview], next_cursor: null } });
  });
});

test("reviews a local plan on desktop and mobile without exposing remote data", async ({ page }) => {
  await page.goto("/organization-plans");
  await expect(page.getByRole("heading", { name: "整理计划工作台" })).toBeVisible();
  await expect(page.getByText("计划 plan-loc", { exact: false })).toBeVisible();
  await expect(page.getByText("remote-private", { exact: false })).toHaveCount(0);
  await expect(page.getByText("pickcode", { exact: false })).toHaveCount(0);

  await page.getByRole("button", { name: "确认本地计划" }).click();
  await expect(page.getByText("已确认本地计划，未执行远端写操作")).toBeVisible();

  const viewport = await page.evaluate(() => ({ width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width);
});
