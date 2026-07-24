import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "live-api.spec.ts",
  webServer: {
    command: "node e2e/live-server.mjs",
    url: "http://127.0.0.1:4176/api/v1/health",
    reuseExistingServer: false,
  },
  use: {
    baseURL: "http://127.0.0.1:4176",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
});
