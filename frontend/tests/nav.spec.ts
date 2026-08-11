import { describe, expect, it } from "vitest";

import { ALL_NAV_VIEWS, NAV_GROUPS } from "../src/nav";

describe("NAV_GROUPS", () => {
  it("组织为三个分组：发现/我的/管理", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual(["发现", "我的", "管理"]);
  });

  it("覆盖全部浏览视图", () => {
    const expected = ["home", "movies", "tv", "popular", "search", "favorites", "history", "library", "organization", "workflows", "notifications", "logs", "settings"] as const;
    for (const view of expected) {
      expect(ALL_NAV_VIEWS).toContain(view);
    }
    expect(new Set(ALL_NAV_VIEWS).size).toBe(ALL_NAV_VIEWS.length);
  });

  it("每个导航项都有 view/label/icon", () => {
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(item.view).toBeTruthy();
        expect(item.label).toBeTruthy();
        expect(item.icon).toBeTruthy();
      }
    }
  });
});
