import type { MovieMetadata } from "./types";

export function mediaTypeOf(movie: MovieMetadata): "movie" | "tv" {
  return movie.media_type === "tv" ? "tv" : "movie";
}

export function mediaKey(movie: MovieMetadata): string {
  return `${mediaTypeOf(movie)}:${movie.tmdb_id}`;
}
