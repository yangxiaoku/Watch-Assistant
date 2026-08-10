import { defineConfig } from "@playwright/test";

/**
 * 针对真实部署实例 (192.168.6.236:8115) 的「真实用户逻辑」验收配置。
 *
 * 与 playwright.config.ts 的区别:
 *  - 不启动任何本地 webServer,直接访问部署目标
 *  - 只运行 e2e/live/** 下的用例
 *  - 凭据只从环境变量读取 (WA_E2E_USER / WA_E2E_PASSWORD),不得写入源码
 *
 * 运行方式:
 *   WA_E2E_USER=admin WA_E2E_PASSWORD=*** npx playwright test -c playwright.deploy.config.ts
 */
const STORAGE_STATE = "test-results/deploy/state.json";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/live/**/*.spec.ts",
  workers: 2,
  fullyParallel: true,
  timeout: 90_000,
  retries: 1,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.WA_TARGET ?? "http://192.168.6.236:8115",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
    locale: "zh-CN",
  },
  outputDir: "test-results/deploy",
  reporter: [["list"], ["html", { outputFolder: "playwright-report/deploy", open: "never" }]],
  projects: [
    // 未登录用户的登录门路径(错误密码、拦截行为),不依赖任何会话
    { name: "no-auth", testMatch: /auth-gate\.spec\.ts/ },
    // 浏览器登录 setup:通过真实 UI 登录门登录并保存会话
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    // 已登录的桌面端核心流程
    {
      name: "desktop",
      testMatch: /deploy-user-flows\.spec\.ts/,
      dependencies: ["setup"],
      use: { storageState: STORAGE_STATE, viewport: { width: 1440, height: 900 } },
    },
    // 已登录的移动端冒烟
    {
      name: "mobile",
      testMatch: /mobile-smoke\.spec\.ts/,
      dependencies: ["setup"],
      use: { storageState: STORAGE_STATE, viewport: { width: 390, height: 844 } },
    },
  ],
});
