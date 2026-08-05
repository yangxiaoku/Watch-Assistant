import { expect, test } from "@playwright/test";

test("keeps the unauthenticated login page inside the viewport", async ({ page }, testInfo) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ status: 401, json: { detail: "unauthorized" } }),
  );
  await page.route("**/api/v1/auth/login", (route) =>
    route.fulfill({ status: 401, json: { detail: "unauthorized" } }),
  );

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "进入观影工作台" })).toBeVisible();

  const metrics = await page.evaluate(() => {
    const rect = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector);
      if (!element) throw new Error(`missing ${selector}`);
      const box = element.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    };
    const boxes = {
      title: rect(".auth-gate h1"),
      description: rect(".auth-gate > p:not(.eyebrow):not(.error-text)"),
      input: rect(".auth-gate input"),
      button: rect(".auth-gate .primary-button"),
    };
    const values = Object.values(boxes);
    const nonOverlapping = values.every((box, index) => values.every((other, otherIndex) =>
      index === otherIndex
      || box.right <= other.left
      || other.right <= box.left
      || box.bottom <= other.top
      || other.bottom <= box.top,
    ));
    const authGate = document.querySelector<HTMLElement>(".auth-gate");
    if (!authGate) throw new Error("missing .auth-gate");
    const title = document.querySelector<HTMLElement>(".auth-gate h1");
    const description = document.querySelector<HTMLElement>(".auth-gate > p:not(.eyebrow):not(.error-text)");
    if (!title || !description) throw new Error("missing login copy");
    return {
      clientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      authGate: rect(".auth-gate"),
      authGateClientWidth: authGate.clientWidth,
      authGateScrollWidth: authGate.scrollWidth,
      copyFits: title.scrollWidth <= title.clientWidth && description.scrollWidth <= description.clientWidth,
      boxes,
      nonOverlapping,
    };
  });

  expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
  expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
  expect(metrics.authGate.left).toBeGreaterThanOrEqual(0);
  expect(metrics.authGate.right).toBeLessThanOrEqual(metrics.clientWidth);
  expect(metrics.authGateScrollWidth).toBeLessThanOrEqual(metrics.authGateClientWidth);
  expect(metrics.authGateScrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
  expect(metrics.copyFits).toBe(true);
  for (const box of Object.values(metrics.boxes)) {
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(metrics.clientWidth);
  }
  expect(metrics.nonOverlapping).toBe(true);

  if (testInfo.project.name === "desktop") {
    await page.screenshot({ path: testInfo.outputPath("login-desktop-1440x900.png"), fullPage: true });
  }
  if (testInfo.project.name === "mobile") {
    await page.screenshot({ path: testInfo.outputPath("login-mobile-390x844.png"), fullPage: true });
  }
  if (testInfo.project.name === "mobile-compact") {
    await page.screenshot({ path: testInfo.outputPath("login-compact-320x568.png"), fullPage: true });
  }

  const loginButton = page.getByRole("button", { name: "登录" });
  await expect(loginButton).toBeEnabled();
  await loginButton.click();
  await expect(page.getByText("密码不正确")).toBeVisible();
});

test("keeps authenticated workbench navigation visible at compact width", async ({ page }, testInfo) => {
  await page.route("**/api/v1/health", (route) => route.fulfill({
    json: {
      status: "ok",
      push_supported: false,
      inspection_supported: false,
      organization_plan_enabled: true,
      organization_execution_enabled: false,
      organization_execution_supported: false,
      strm_capabilities: { full: false, incremental: false, cleanup: false, playback: false, playback_contract_verified: false },
    },
  }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: null } }));

  await page.goto("/workbench");
  await expect(page.getByRole("heading", { name: "观影工作台" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "主导航" });
  for (const label of ["工作台", "首页", "电影", "剧集", "热门", "收藏", "记录", "整理", "整理历史", "媒体库"]) {
    await expect(nav.getByRole("button", { name: label, exact: true })).toBeVisible();
  }

  const layout = await page.evaluate(() => {
    const element = document.querySelector<HTMLElement>(".primary-nav");
    if (!element) throw new Error("missing primary navigation");
    const buttons = Array.from(element.querySelectorAll<HTMLElement>("button")).map((button) => {
      const box = button.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    });
    return { width: window.innerWidth, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, buttons };
  });
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  for (const button of layout.buttons) {
    expect(button.left).toBeGreaterThanOrEqual(-1);
    expect(button.right).toBeLessThanOrEqual(layout.width + 1);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  if (testInfo.project.name === "mobile-compact") {
    await page.screenshot({ path: testInfo.outputPath("workbench-navigation-compact.png"), fullPage: true });
  }
});
