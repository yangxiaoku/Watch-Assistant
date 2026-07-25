import { describe, expect, it } from "vitest";

import { catalogRoutePath, extractBrowseView, extractMediaRoute, parseCatalogRoute } from "../src/router";

describe("media route seasons", () => {
  it("restores a positive TV season from the URL", () => {
    expect(extractMediaRoute("/tv/1399?season=2")).toEqual({ mediaType: "tv", tmdbId: 1399, seasonNumber: 2 });
    expect(extractMediaRoute("/tv/1399")).toEqual({ mediaType: "tv", tmdbId: 1399 });
    expect(extractMediaRoute("/tv/1399?season=0")).toEqual({ mediaType: "tv", tmdbId: 1399, seasonNumber: 0 });
  });

  it("ignores season parameters on movie routes", () => {
    expect(extractMediaRoute("/movie/27205?season=2")).toEqual({ mediaType: "movie", tmdbId: 27205 });
  });
});

describe("settings route", () => {
  it("restores the settings view", () => {
    expect(extractBrowseView("/settings")).toBe("settings");
  });
});

describe("catalog routes", () => {
  it("round-trips search and catalog filters through the URL", () => {
    const route = parseCatalogRoute("/movies?page=2&genre=28&year=2024&sort=rating");
    expect(route).toEqual({ view: "movies", query: "", page: 2, genreId: 28, year: 2024, sort: "rating" });
    expect(catalogRoutePath(route!)).toBe("/movies?page=2&genre=28&year=2024&sort=rating");

    expect(parseCatalogRoute("/search?query=the%20bear&page=3")).toEqual({
      view: "search",
      query: "the bear",
      page: 3,
      sort: "popular",
    });
  });

  it("clamps invalid catalog pages to the 500-page boundary", () => {
    expect(parseCatalogRoute("/popular?page=501")?.page).toBe(500);
    expect(parseCatalogRoute("/popular?page=0")?.page).toBe(1);
    expect(catalogRoutePath({ view: "popular", query: "", page: 501, sort: "popular" })).toBe("/popular?page=500");
  });

  it("drops out-of-range filters and trims search terms", () => {
    expect(parseCatalogRoute("/movies?genre=0&year=9999")).toEqual({
      view: "movies",
      query: "",
      page: 1,
      sort: "popular",
    });
    expect(parseCatalogRoute("/search?query=%20%20the%20bear%20%20")?.query).toBe("the bear");
    expect(parseCatalogRoute("/search?query=%20%20")?.query).toBe("");
  });
});
