import { describe, expect, it } from "vitest";

import { extractMediaRoute } from "../src/router";

describe("media route seasons", () => {
  it("restores a positive TV season from the URL", () => {
    expect(extractMediaRoute("/tv/1399?season=2")).toEqual({ mediaType: "tv", tmdbId: 1399, seasonNumber: 2 });
    expect(extractMediaRoute("/tv/1399")).toEqual({ mediaType: "tv", tmdbId: 1399 });
  });

  it("ignores season parameters on movie routes", () => {
    expect(extractMediaRoute("/movie/27205?season=2")).toEqual({ mediaType: "movie", tmdbId: 27205 });
  });
});
