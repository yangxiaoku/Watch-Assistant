export function extractMovieId(path: string): number | null {
  const match = path.match(/^\/movie\/(\d+)(?:-|\/|$)/);
  return match ? Number(match[1]) : null;
}

export function navigateToMovie(tmdbId: number): void {
  window.history.pushState({}, "", `/movie/${tmdbId}`);
}

export interface MediaRoute {
  mediaType: "movie" | "tv";
  tmdbId: number;
  seasonNumber?: number;
  resourcePage?: number;
  resourceKind?: "all" | "magnet" | "115_share";
  resourceQuality?: "all" | "4k" | "1080p" | "720p" | "subtitle";
  resourceQuery?: string;
  resourceSort?: "comprehensive" | "relevance" | "completeness" | "size" | "seeders";
  resourcePageSize?: 25 | 50 | 100;
}

export function extractMediaRoute(path: string): MediaRoute | null {
  const [pathname, query = ""] = path.split("?", 2);
  const match = pathname.match(/^\/(movie|tv)\/(\d+)(?:-|\/|$)/);
  if (!match) return null;
  const mediaType = match[1] as "movie" | "tv";
  const rawSeason = new URLSearchParams(query).get("season");
  const params = new URLSearchParams(query);
  const season = rawSeason === null ? null : Number(rawSeason);
  const rawPage = Number(params.get("resource_page") ?? "1");
  const rawPageSize = Number(params.get("resource_page_size") ?? "25");
  const resourceKind = params.get("resource_kind");
  const resourceQuality = params.get("resource_quality");
  const resourceSort = params.get("resource_sort");
  const resourcePage = Number.isInteger(rawPage) && rawPage >= 1 && rawPage <= 500 ? rawPage : 1;
  const resourcePageSize = rawPageSize === 50 || rawPageSize === 100 ? rawPageSize : 25;
  const hasResourceParams = ["resource_page", "resource_kind", "resource_quality", "resource_query", "resource_sort", "resource_page_size"].some((key) => params.has(key));
  return {
    mediaType,
    tmdbId: Number(match[2]),
    ...(mediaType === "tv" && rawSeason !== null && /^\d+$/.test(rawSeason) && Number.isInteger(season) && season >= 0 ? { seasonNumber: season } : {}),
    ...(hasResourceParams ? {
      resourcePage,
      resourceKind: resourceKind === "magnet" || resourceKind === "115_share" ? resourceKind : "all",
      resourceQuality: resourceQuality === "4k" || resourceQuality === "1080p" || resourceQuality === "720p" || resourceQuality === "subtitle" ? resourceQuality : "all",
      resourceQuery: (params.get("resource_query") ?? "").trim(),
      resourceSort: resourceSort === "relevance" || resourceSort === "completeness" || resourceSort === "size" || resourceSort === "seeders" ? resourceSort : "comprehensive",
      resourcePageSize,
    } : {}),
  };
}

export interface MediaResourceRouteState {
  page: number;
  kind: "all" | "magnet" | "115_share";
  quality: "all" | "4k" | "1080p" | "720p" | "subtitle";
  query: string;
  sort: "comprehensive" | "relevance" | "completeness" | "size" | "seeders";
  pageSize: 25 | 50 | 100;
}

export function mediaRoutePath(mediaType: "movie" | "tv", tmdbId: number, seasonNumber?: number, resources?: MediaResourceRouteState): string {
  const params = new URLSearchParams();
  if (mediaType === "tv" && seasonNumber !== undefined) params.set("season", String(seasonNumber));
  if (resources) {
    if (resources.page > 1) params.set("resource_page", String(resources.page));
    if (resources.kind !== "all") params.set("resource_kind", resources.kind);
    if (resources.quality !== "all") params.set("resource_quality", resources.quality);
    if (resources.query.trim()) params.set("resource_query", resources.query.trim());
    if (resources.sort !== "comprehensive") params.set("resource_sort", resources.sort);
    if (resources.pageSize !== 25) params.set("resource_page_size", String(resources.pageSize));
  }
  const query = params.toString();
  return `/${mediaType}/${tmdbId}${query ? `?${query}` : ""}`;
}

export function navigateToMedia(mediaType: "movie" | "tv", tmdbId: number, seasonNumber?: number, resources?: MediaResourceRouteState, replace = false): void {
  const path = mediaRoutePath(mediaType, tmdbId, seasonNumber, resources);
  const currentState = window.history.state ?? {};
  if (replace) {
    window.history.replaceState(currentState, "", path);
    return;
  }
  const currentBackDelta = Number.isInteger(currentState.catalogBackDelta) ? currentState.catalogBackDelta : 0;
  window.history.pushState({
    ...currentState,
    ...(currentState.catalogDetailEntry ? { catalogBackDelta: currentBackDelta + 1 } : {}),
  }, "", path);
}

export function navigateHome(): void {
  window.history.pushState({}, "", "/");
}

export type BrowseView = "home" | "workbench" | "movies" | "tv" | "popular" | "favorites" | "history" | "search" | "settings" | "organization-plans" | "organization-history" | "library" | "workflows" | "notifications";
export type CatalogView = "movies" | "tv" | "popular" | "search";
export type CatalogSort = "popular" | "rating" | "release";
export interface CatalogRoute {
  view: CatalogView;
  query: string;
  page: number;
  genreId?: number;
  year?: number;
  sort: CatalogSort;
}

export const MAX_CATALOG_PAGES = 500;

export function clampCatalogPage(value: number): number {
  return Math.min(MAX_CATALOG_PAGES, Math.max(1, Number.isInteger(value) ? value : 1));
}

const VIEW_PATHS: Record<Exclude<BrowseView, "search">, string> = {
  home: "/",
  workbench: "/workbench",
  movies: "/movies",
  tv: "/tv",
  popular: "/popular",
  favorites: "/favorites",
  history: "/history",
  settings: "/settings",
  "organization-plans": "/organization-plans",
  "organization-history": "/organization-history",
  library: "/library",
  workflows: "/workflows",
  notifications: "/notifications",
};

export function navigateToView(view: Exclude<BrowseView, "search">, replace = false): void {
  if (replace) window.history.replaceState(window.history.state, "", VIEW_PATHS[view]);
  else window.history.pushState({}, "", VIEW_PATHS[view]);
}

export function parseCatalogRoute(path: string): CatalogRoute | null {
  const [pathname, queryString = ""] = path.split("?", 2);
  const view = extractBrowseView(pathname);
  if (view !== "movies" && view !== "tv" && view !== "popular" && view !== "search") return null;
  const params = new URLSearchParams(queryString);
  const rawPage = Number(params.get("page") ?? "1");
  const rawGenre = params.get("genre");
  const rawYear = params.get("year");
  const rawSort = params.get("sort");
  const parsedGenre = rawGenre !== null && /^\d+$/.test(rawGenre) ? Number(rawGenre) : undefined;
  const genreId = parsedGenre !== undefined && parsedGenre >= 1 ? parsedGenre : undefined;
  const parsedYear = rawYear !== null && /^\d{4}$/.test(rawYear) ? Number(rawYear) : undefined;
  const year = parsedYear !== undefined && parsedYear >= 1900 && parsedYear <= 2100 ? parsedYear : undefined;
  const sort: CatalogSort = rawSort === "rating" || rawSort === "release" ? rawSort : "popular";
  return {
    view,
    query: (params.get("query") ?? params.get("q") ?? "").trim(),
    page: clampCatalogPage(rawPage),
    ...(genreId !== undefined ? { genreId } : {}),
    ...(year !== undefined ? { year } : {}),
    sort,
  };
}

export function catalogRoutePath(route: CatalogRoute): string {
  const params = new URLSearchParams();
  if (route.page > 1) params.set("page", String(clampCatalogPage(route.page)));
  if (route.query.trim()) params.set("query", route.query.trim());
  if (route.genreId !== undefined) params.set("genre", String(route.genreId));
  if (route.year !== undefined) params.set("year", String(route.year));
  if (route.sort !== "popular") params.set("sort", route.sort);
  const query = params.toString();
  return `${route.view === "search" ? "/search" : `/${route.view}`}${query ? `?${query}` : ""}`;
}

export function navigateToCatalog(route: CatalogRoute, replace = false, state: Record<string, unknown> = {}): void {
  const historyState = { ...state, catalog: route };
  if (replace) window.history.replaceState(historyState, "", catalogRoutePath(route));
  else window.history.pushState(historyState, "", catalogRoutePath(route));
}

export function navigateToSearch(query: string, page = 1): void {
  navigateToCatalog({ view: "search", query, page: clampCatalogPage(page), sort: "popular" });
}

export function extractBrowseView(path: string): BrowseView {
  const pathname = path.split("?", 1)[0];
  if (pathname === "/movies") return "movies";
  if (pathname === "/workbench") return "workbench";
  if (pathname === "/tv") return "tv";
  if (pathname === "/popular") return "popular";
  if (pathname === "/favorites") return "favorites";
  if (pathname === "/history") return "history";
  if (pathname === "/search") return "search";
  if (pathname === "/settings") return "settings";
  if (pathname === "/organization-plans") return "organization-plans";
  if (pathname === "/organization-history") return "organization-history";
  if (pathname === "/library") return "library";
  if (pathname === "/workflows") return "workflows";
  if (pathname === "/notifications") return "notifications";
  return "home";
}
