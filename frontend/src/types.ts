export type ResourceKind = "magnet" | "115_share";
export type TaskState =
  | "queued"
  | "submitting"
  | "accepted"
  | "needs_auth"
  | "failed"
  | "uncertain";

export interface MovieMetadata {
  tmdb_id: number;
  title: string;
  original_title: string | null;
  release_year: number | null;
  overview: string | null;
  poster_path: string | null;
}

export interface ResourceSummary {
  resource_id: string;
  kind: ResourceKind;
  name: string;
  size_bytes: number | null;
  seeders: number | null;
  source: string;
  captured_at: string;
}

export interface SearchResponse {
  movie: MovieMetadata;
  results: ResourceSummary[];
  warnings: string[];
  cached: boolean;
  cache_age_seconds: number | null;
}

export interface TaskResponse {
  id: string;
  resource_id: string | null;
  action: "offline_download" | "save_share";
  state: TaskState;
  attempts: number;
  remote_ref: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
}
