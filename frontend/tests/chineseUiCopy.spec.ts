import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sourceFiles = [
  "../index.html",
  "../src/views/HomeView.vue",
  "../src/views/CollectionView.vue",
  "../src/views/LibraryView.vue",
  "../src/views/SearchView.vue",
  "../src/views/OrganizationWorkbenchView.vue",
  "../src/views/LibraryWorkbenchView.vue",
  "../src/views/SettingsView.vue",
  "../src/views/OrganizationView.vue",
  "../src/views/LogsView.vue",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));

const forbiddenVisibleCopy = [
  "TRENDING NOW",
  "TV ON THE AIR",
  "COMING SOON",
  "TOP RATED",
  "YOUR LIBRARY",
  "EXPLORE TMDB",
  "SEARCH RESULTS",
  "LOCAL REVIEW",
  "WORKSPACE SETTINGS",
  "SYSTEM SNAPSHOT",
  "MANAGED CONNECTIONS",
  "EVENT STREAM",
  "CONTENT SAFETY",
  "RESOURCE INSPECTION",
  "P115 CONNECTOR",
  ">Release<",
];

describe("中文用户界面文案", () => {
  it("does not reintroduce known non-technical English labels", () => {
    const combined = sourceFiles.join("\n");
    for (const copy of forbiddenVisibleCopy) {
      expect(combined).not.toContain(copy);
    }
  });
});
