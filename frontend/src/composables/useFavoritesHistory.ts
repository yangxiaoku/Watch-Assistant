import { computed, ref } from "vue";
import { mediaKey } from "../media";
import type { MovieMetadata } from "../types";

const FAVORITES_KEY = "watch-assistant:favorites";
const HISTORY_KEY = "watch-assistant:history";

function readStoredMovies(key: string): MovieMetadata[] {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "[]");
    return Array.isArray(value)
      ? value.filter((movie) => Number.isInteger(movie?.tmdb_id) && typeof movie?.title === "string")
      : [];
  } catch {
    return [];
  }
}

function storeMovies(key: string, movies: MovieMetadata[]) {
  localStorage.setItem(key, JSON.stringify(movies));
}

/** 收藏与观看记录，持久化在 localStorage。 */
export function useFavoritesHistory() {
  const favorites = ref<MovieMetadata[]>(readStoredMovies(FAVORITES_KEY));
  const history = ref<MovieMetadata[]>(readStoredMovies(HISTORY_KEY));
  const favoriteIds = computed(() => new Set(favorites.value.map(mediaKey)));

  function toggleFavorite(movie: MovieMetadata) {
    const key = mediaKey(movie);
    favorites.value = favoriteIds.value.has(key)
      ? favorites.value.filter((item) => mediaKey(item) !== key)
      : [movie, ...favorites.value].slice(0, 100);
    storeMovies(FAVORITES_KEY, favorites.value);
  }

  function recordHistory(movie: MovieMetadata) {
    history.value = [movie, ...history.value.filter((item) => mediaKey(item) !== mediaKey(movie))].slice(0, 50);
    storeMovies(HISTORY_KEY, history.value);
  }

  return { favorites, history, favoriteIds, toggleFavorite, recordHistory };
}
