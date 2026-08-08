import { describe, expect, it } from "vitest";

import { catalogRoutePath, extractBrowseView, extractMediaRoute, mediaRoutePath, navigateToMedia, navigateToView, parseCatalogRoute } from "../src/router";

describe("media route seasons", () => {
  it("replaces a season URL while preserving detail history state", () => {
    window.history.pushState({ catalog: { view: "tv", page: 2 }, catalogDetailEntry: true, catalogScrollY: 380 }, "", "/tv/1399");
    const historyLength = window.history.length;

    navigateToMedia("tv", 1399, 2, undefined, true);

    expect(window.history.length).toBe(historyLength);
    expect(window.location.pathname + window.location.search).toBe("/tv/1399?season=2");
    expect(window.history.state).toMatchObject({ catalog: { view: "tv", page: 2 }, catalogDetailEntry: true, catalogScrollY: 380 });
  });

  it("restores a positive TV season from the URL", () => {
    expect(extractMediaRoute("/tv/1399?season=2")).toEqual({ mediaType: "tv", tmdbId: 1399, seasonNumber: 2 });
    expect(extractMediaRoute("/tv/1399")).toEqual({ mediaType: "tv", tmdbId: 1399 });
    expect(extractMediaRoute("/tv/1399?season=0")).toEqual({ mediaType: "tv", tmdbId: 1399, seasonNumber: 0 });
  });

  it("ignores season parameters on movie routes", () => {
    expect(extractMediaRoute("/movie/27205?season=2")).toEqual({ mediaType: "movie", tmdbId: 27205 });
  });

  it("restores resource pagination state and keeps season zero", () => {
    expect(extractMediaRoute("/tv/1399?season=0&resource_page=3&resource_kind=magnet&resource_quality=1080p&resource_query=%20bear%20&resource_sort=size&resource_page_size=100")).toMatchObject({
      mediaType: "tv", tmdbId: 1399, seasonNumber: 0, resourcePage: 3, resourceKind: "magnet", resourceQuality: "1080p", resourceQuery: "bear", resourceSort: "size", resourcePageSize: 100,
    });
    expect(extractMediaRoute("/movie/1?resource_page=501")).toMatchObject({ resourcePage: 1 });
    expect(mediaRoutePath("tv", 1399, 0, { page: 3, kind: "magnet", quality: "1080p", query: "bear", sort: "size", pageSize: 100 })).toBe("/tv/1399?season=0&resource_page=3&resource_kind=magnet&resource_quality=1080p&resource_query=bear&resource_sort=size&resource_page_size=100");
  });
});

describe("settings route", () => {
  it("restores the settings view", () => {
    expect(extractBrowseView("/settings")).toBe("settings");
  });

  it("restores the organization view", () => {
    expect(extractBrowseView("/organization")).toBe("organization");
  });

  it("falls back to home for a removed workbench entry", () => {
    expect(extractBrowseView("/workbench")).toBe("home");
  });
});

describe("catalog routes", () => {
  it("replaces an invalid search route without adding history", () => {
    window.history.pushState({ source: "catalog" }, "", "/search?query=");
    const historyLength = window.history.length;

    navigateToView("home", true);

    expect(window.history.length).toBe(historyLength);
    expect(window.location.pathname).toBe("/");
    expect(window.history.state).toMatchObject({ source: "catalog" });
  });

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
