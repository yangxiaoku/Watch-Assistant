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
}

export function extractMediaRoute(path: string): MediaRoute | null {
  const [pathname, query = ""] = path.split("?", 2);
  const match = pathname.match(/^\/(movie|tv)\/(\d+)(?:-|\/|$)/);
  if (!match) return null;
  const mediaType = match[1] as "movie" | "tv";
  const rawSeason = new URLSearchParams(query).get("season");
  const season = rawSeason === null ? null : Number(rawSeason);
  return {
    mediaType,
    tmdbId: Number(match[2]),
    ...(mediaType === "tv" && rawSeason !== null && /^\d+$/.test(rawSeason) && Number.isInteger(season) && season >= 0 ? { seasonNumber: season } : {}),
  };
}

export function navigateToMedia(mediaType: "movie" | "tv", tmdbId: number, seasonNumber?: number): void {
  const query = mediaType === "tv" && seasonNumber !== undefined ? `?season=${seasonNumber}` : "";
  window.history.pushState({}, "", `/${mediaType}/${tmdbId}${query}`);
}

export function navigateHome(): void {
  window.history.pushState({}, "", "/");
}

export type BrowseView = "home" | "movies" | "tv" | "popular" | "favorites" | "history" | "search" | "settings";
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
  movies: "/movies",
  tv: "/tv",
  popular: "/popular",
  favorites: "/favorites",
  history: "/history",
  settings: "/settings",
};

export function navigateToView(view: Exclude<BrowseView, "search">): void {
  window.history.pushState({}, "", VIEW_PATHS[view]);
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
  const genreId = rawGenre !== null && /^\d+$/.test(rawGenre) ? Number(rawGenre) : undefined;
  const year = rawYear !== null && /^\d{4}$/.test(rawYear) ? Number(rawYear) : undefined;
  const sort: CatalogSort = rawSort === "rating" || rawSort === "release" ? rawSort : "popular";
  return {
    view,
    query: params.get("query") ?? params.get("q") ?? "",
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
  if (pathname === "/tv") return "tv";
  if (pathname === "/popular") return "popular";
  if (pathname === "/favorites") return "favorites";
  if (pathname === "/history") return "history";
  if (pathname === "/search") return "search";
  if (pathname === "/settings") return "settings";
  return "home";
}
