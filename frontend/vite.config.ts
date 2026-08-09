import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

// The panel lets users configure any self-hosted API base, so the userscript
// must be allowed to reach any host. @connect * is a build-time directive for
// the userscript manager; the server itself still requires a Bearer token.
const userscriptMetadata = `// ==UserScript==
// @name         Watch Assistant TMDB Panel
// @namespace    local.watch-assistant
// @match        https://www.themoviedb.org/movie/*
// @grant        GM.xmlHttpRequest
// @grant        GM.getValue
// @grant        GM.setValue
// @connect      *
// ==/UserScript==`;

export default defineConfig({
  plugins: [
    vue(),
    {
      name: "userscript-metadata",
      generateBundle(_options, bundle) {
        for (const output of Object.values(bundle)) {
          if (output.type === "chunk" && output.name === "userscript") {
            output.code = `${userscriptMetadata}\n${output.code}`;
          }
        }
      },
    },
  ],
  build: {
    rollupOptions: {
      input: {
        app: fileURLToPath(new URL("./index.html", import.meta.url)),
        userscript: fileURLToPath(new URL("./src/userscript.ts", import.meta.url)),
      },
      output: {
        entryFileNames: (chunk) =>
          chunk.name === "userscript" ? "watch-assistant.user.js" : "assets/[name]-[hash].js",
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts", "tests/**/*.spec.ts"],
  },
});
