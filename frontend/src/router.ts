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

export type BrowseView = "home" | "movies" | "tv" | "popular" | "favorites" | "history" | "search";

const VIEW_PATHS: Record<Exclude<BrowseView, "search">, string> = {
  home: "/",
  movies: "/movies",
  tv: "/tv",
  popular: "/popular",
  favorites: "/favorites",
  history: "/history",
};

export function navigateToView(view: Exclude<BrowseView, "search">): void {
  window.history.pushState({}, "", VIEW_PATHS[view]);
}

export function navigateToSearch(query: string): void {
  window.history.pushState({}, "", `/search?q=${encodeURIComponent(query)}`);
}

export function extractBrowseView(path: string): BrowseView {
  if (path === "/movies") return "movies";
  if (path === "/tv") return "tv";
  if (path === "/popular") return "popular";
  if (path === "/favorites") return "favorites";
  if (path === "/history") return "history";
  if (path === "/search") return "search";
  return "home";
}
