import { expect, test } from "@playwright/test";

const movie = {
  tmdb_id: 27205,
  media_type: "movie",
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "梦境中的梦境。",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [878],
  vote_average: 8.4,
};

test("routes push actions by resource capability", async ({ page }) => {
  let taskPostCount = 0;
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({ json: { status: "ok", push_supported: false, push_capabilities: { magnet: true, share: false } } }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ json: { authenticated: true, via_bearer: false, csrf_token: "csrf-test" } }),
  );
  await page.route("**/api/v1/search", (route) => route.fulfill({
    json: {
      movie,
      results: [
        { resource_id: "magnet-1", kind: "magnet", name: "Inception 1080P", size_bytes: null, seeders: 4, source: "test", captured_at: "2026-07-24T10:00:00Z" },
        { resource_id: "share-1", kind: "115_share", name: "115 分享资源", size_bytes: null, seeders: null, source: "test", captured_at: "2026-07-24T10:00:00Z" },
      ],
      warnings: [],
      cached: false,
      cache_age_seconds: null,
    },
  }));
  await page.route("**/api/v1/tasks", async (route) => {
    taskPostCount += 1;
    return route.fulfill({
      json: {
        id: "task-1",
        resource_id: "magnet-1",
        action: "offline_download",
        state: "queued",
        attempts: 0,
        remote_ref: null,
        error_code: null,
        error_message: null,
        created_at: "2026-07-24T10:00:00Z",
        updated_at: "2026-07-24T10:00:00Z",
        submitted_at: null,
      },
    });
  });

  await page.goto("/movie/27205");
  await expect(page.getByRole("heading", { name: "盗梦空间" })).toBeVisible();
  await expect(page.getByText("磁力云下载可用，115 分享转存尚未验证")).toBeVisible();
  await expect(page.getByText("TgtoDrive 推送契约尚未验证，推送按钮已禁用。")).toHaveCount(0);
  const resourceRows = page.locator(".resource-table:visible tbody tr, .resource-cards:visible article");
  const magnetRow = resourceRows.filter({ hasText: "Inception 1080P" });
  const shareRow = resourceRows.filter({ hasText: "115 分享资源" });
  const magnetButton = magnetRow.getByRole("button", { name: "推送下载" });
  const shareButton = shareRow.locator("button.push-button");

  await expect(magnetButton).toBeEnabled();
  await expect(shareButton).toBeDisabled();
  await expect(shareButton).toHaveAttribute("title", "115 分享转存尚未验证");
  await shareButton.evaluate((button) => (button as HTMLButtonElement).click());
  await expect.poll(() => taskPostCount).toBe(0);

  await magnetButton.click();
  await expect.poll(() => taskPostCount).toBe(1);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});
