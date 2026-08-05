import { describe, expect, it } from "vitest";

import playwrightConfig from "../playwright.config";

describe("Playwright web server contract", () => {
  it("keeps mock browser concurrency within the Vite server budget", () => {
    expect(playwrightConfig.workers).toBe(2);
  });

  it("starts Vite directly without package-manager argument forwarding", () => {
    const webServer = Array.isArray(playwrightConfig.webServer)
      ? playwrightConfig.webServer[0]
      : playwrightConfig.webServer;

    expect(webServer).toMatchObject({
      command: "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4175",
      url: "http://127.0.0.1:4175",
    });
    expect(webServer?.command).not.toContain("npm run dev --");
    expect(webServer?.command).not.toContain("vite -- --host");
  });
});
