import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "**/live-api.spec.ts",
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4175",
    url: "http://127.0.0.1:4175",
    reuseExistingServer: true,
  },
  use: {
    baseURL: "http://127.0.0.1:4175",
  },
  projects: [
    { name: "desktop-wide", use: { viewport: { width: 1920, height: 1080 } } },
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
    { name: "mobile-wide", use: { viewport: { width: 430, height: 932 } } },
  ],
});
