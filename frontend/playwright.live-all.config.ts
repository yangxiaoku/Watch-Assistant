import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/live",
  testMatch: "all-features.spec.ts",
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 30_000 },
  use: {
    baseURL: "http://192.168.6.236:8115",
  },
  reporter: [["list"], ["html", { outputFolder: "test-results/live-all/html", open: "never" }]],
});
