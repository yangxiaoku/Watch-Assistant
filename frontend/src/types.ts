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
  adult?: boolean;
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
  size_source?: "pansou" | "inspection" | null;
  seeders_source?: "pansou" | null;
  seeders_observed_at?: string | null;
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
  hidden_total?: number;
}

export type ResourceQuality = "4k" | "1080p" | "720p" | "subtitle";
export type ResourceSort = "comprehensive" | "relevance" | "completeness" | "size" | "seeders";

export interface ResourceFacets {
  magnet: number;
  share: number;
  "4k": number;
  "1080p": number;
  "720p": number;
  subtitle: number;
}

export interface ResourcePageResponse {
  items: ResourceSummary[];
  page: number;
  page_size: 25 | 50 | 100;
  total: number;
  total_pages: number;
  facets: ResourceFacets;
  snapshot_revision: string;
  hidden_total?: number;
}

export interface InspectionResult {
  resource_id: string;
  infohash: string | null;
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
  push_capabilities?: {
    magnet: boolean;
    share: boolean;
  };
  inspection_supported?: boolean;
}

export type LogLevel = "DEBUG" | "ERROR" | "WARNING" | "INFO";
export type LogCategory = "system" | "search" | "cache" | "inspection" | "p115" | "security";

export interface SettingsOverviewResponse {
  release: string;
  uptime_seconds: number;
  database_size_bytes: number;
  capabilities: {
    inspection: boolean;
    magnet: boolean;
    share: boolean;
  };
}

export interface LoggingSettingsResponse {
  revision: number;
  level: LogLevel;
  retention_days: number;
  max_file_mb: number;
}

export interface ContentPolicyResponse {
  hide_adult_media: boolean;
  hide_suspicious_resources: boolean;
  hide_low_quality_resources: boolean;
  blocked_keywords: string[];
  revision: number;
}

export interface PatchContentPolicyRequest {
  revision: number;
  hide_adult_media?: boolean;
  hide_suspicious_resources?: boolean;
  hide_low_quality_resources?: boolean;
  blocked_keywords?: string[];
}

export interface PatchLoggingSettingsRequest {
  revision: number;
  level: LogLevel;
  retention_days: number;
  max_file_mb: number;
}

export interface P115SettingsResponse {
  enabled: boolean;
  ready: boolean;
  capabilities: {
    magnet: boolean;
    share: boolean;
  };
  cookie: {
    source: "tgtodrive";
    configured: boolean;
    structure_valid: boolean;
    sync_status: "success" | "failed" | "unknown";
    last_sync_at: string | null;
  };
  target_configured: boolean;
  max_concurrency: number;
}

export interface P115ValidationResponse {
  status: "ready" | "needs_auth" | "unavailable";
  checked_at: string;
}

export interface LogEntry {
  id: number;
  timestamp: string;
  level: LogLevel;
  category: LogCategory;
  message: string;
}

export interface LogsResponse {
  items: LogEntry[];
  next_cursor: number | null;
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
