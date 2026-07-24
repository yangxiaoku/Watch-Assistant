export function extractMovieId(path: string): number | null {
  const match = path.match(/^\/movie\/(\d+)(?:-|\/|$)/);
  return match ? Number(match[1]) : null;
}

export function navigateToMovie(tmdbId: number): void {
  window.history.pushState({}, "", `/movie/${tmdbId}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
