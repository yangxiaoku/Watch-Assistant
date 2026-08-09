import { describe, expect, it } from "vitest";

import { extractMovieId, formatUserscriptError, shouldSearchMovie } from "../src/userscript";

describe("TMDB userscript helpers", () => {
  it("extracts movie ids from TMDB SPA routes", () => {
    expect(extractMovieId("/movie/12345-inception")).toBe(12345);
    expect(extractMovieId("/tv/12345-show")).toBeNull();
  });

  it("deduplicates the same movie within the ten minute window", () => {
    const cache = new Map<number, number>();
    expect(shouldSearchMovie(12345, cache, 1_000)).toBe(true);
    cache.set(12345, 1_000);
    expect(shouldSearchMovie(12345, cache, 1_000 + 9 * 60_000)).toBe(false);
    expect(shouldSearchMovie(12345, cache, 1_000 + 10 * 60_000)).toBe(true);
  });

  it("keeps failures inside a compact panel message", () => {
    expect(formatUserscriptError(new Error("https://secret.internal/path"))).toBe(
      "观影资源暂时不可用，请稍后重试。",
    );
  });

  it("maps known transport failures to actionable guidance", () => {
    expect(formatUserscriptError(new Error("network error"))).toContain("API 地址");
    expect(formatUserscriptError(new Error("timeout"))).toContain("API 地址");
    expect(formatUserscriptError(new Error("request failed"))).toContain("Token");
    expect(formatUserscriptError(new Error("GM API unavailable"))).toContain("Tampermonkey");
  });
});
