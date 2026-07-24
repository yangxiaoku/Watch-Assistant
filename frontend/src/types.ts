export type ResourceKind = "magnet" | "115_share";
export type InspectionResultStatus = "verified" | "timeout" | "failed" | "unsupported";
export type InspectionBatchStatus = "queued" | "running" | "completed" | "partial" | "failed";
export type TaskState =
  | "queued"
  | "submitting"
  | "accepted"
  | "needs_auth"
  | "failed"
  | "uncertain";

export interface MovieMetadata {
  tmdb_id: number;
  media_type?: "movie" | "tv";
  title: string;
  original_title: string | null;
  release_year: number | null;
  overview: string | null;
  poster_path: string | null;
  backdrop_path?: string | null;
  genre_ids?: number[];
  vote_average: number | null;
  seasons?: SeasonMetadata[];
}

export interface SeasonMetadata {
  season_number: number;
  name: string;
  episode_count: number;
  air_date: string | null;
  poster_path: string | null;
}

export interface MovieCollectionResponse {
  results: MovieMetadata[];
  page: number;
  total_pages: number;
  total_results: number;
}

export interface HomeCatalogResponse {
  popular: MovieMetadata[];
  now_playing: MovieMetadata[];
  upcoming: MovieMetadata[];
  top_rated: MovieMetadata[];
  tv_popular: MovieMetadata[];
  tv_on_the_air: MovieMetadata[];
  tv_top_rated: MovieMetadata[];
}

export interface ResourceSummary {
  resource_id: string;
  kind: ResourceKind;
  name: string;
  size_bytes: number | null;
  seeders: number | null;
  source: string;
  captured_at: string;
  rank_score?: number | null;
  relevance_score?: number | null;
  completeness_score?: number | null;
  inspection_status?: InspectionResultStatus | "running" | "queued" | null;
  video_file_count?: number | null;
  subtitle_count?: number | null;
  sample_count?: number | null;
}

export interface SearchRequest {
  tmdb_id: number;
  media_type: "movie" | "tv";
  refresh: boolean;
  season_number?: number;
}

export interface SearchResponse {
  movie: MovieMetadata;
  results: ResourceSummary[];
  warnings: string[];
  cached: boolean;
  cache_age_seconds: number | null;
  selected_season?: number | null;
}

export interface InspectionResult {
  resource_id: string;
  infohash: string;
  status: InspectionResultStatus;
  total_size_bytes: number;
  file_count: number;
  video_file_count: number;
  video_size_bytes: number;
  subtitle_count: number;
  sample_count: number;
  largest_video_name: string | null;
  content_summary: string | null;
  error_code: string | null;
}

export interface InspectionBatchResponse {
  batch_id: string;
  status: InspectionBatchStatus;
  submitted_count: number;
  completed_count: number;
  results: InspectionResult[];
}

export interface HealthResponse {
  status: string;
  push_supported: boolean;
  inspection_supported?: boolean;
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
