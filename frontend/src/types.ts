export type ResourceKind = "magnet" | "115_share";
export type InspectionResultStatus = "verified" | "timeout" | "failed" | "unsupported";
export type InspectionBatchStatus = "queued" | "running" | "completed" | "partial" | "failed";
export type ProwlarrSettingsSource = "managed" | "environment" | "none";
export type ProwlarrVerificationStatus = "available" | "unavailable" | "disabled";
export type ProwlarrHealthState =
  | "disabled"
  | "not_configured"
  | "unverified"
  | "available"
  | "degraded"
  | "backoff"
  | "open_circuit";

export interface SearchSourceStateResponse {
  enabled: boolean;
  configured: boolean;
  status: "disabled" | "configured" | "unavailable";
  message_code: string | null;
  state: ProwlarrHealthState | null;
  message_zh: string | null;
  reason_code: string | null;
  reason_zh: string | null;
  checked_at: string | null;
  retry_after_seconds: number | null;
  consecutive_failures: number;
}

export interface SearchSourcesResponse {
  pansou: SearchSourceStateResponse;
  prowlarr: SearchSourceStateResponse;
}

export interface ProwlarrSettingsResponse {
  source: ProwlarrSettingsSource;
  enabled: boolean;
  configured: boolean;
  base_url: string | null;
  api_key_configured: boolean;
  api_key_source: ProwlarrSettingsSource;
  last_updated_at: string | null;
  revision: number;
  health_state?: ProwlarrHealthState | null;
  health_message_code?: string | null;
  health_message_zh?: string | null;
  health_reason_code?: string | null;
  health_reason_zh?: string | null;
  health_checked_at?: string | null;
  health_retry_after_seconds?: number | null;
  health_consecutive_failures?: number;
}

export interface PatchProwlarrSettingsRequest {
  enabled?: boolean | null;
  base_url?: string | null;
  api_key?: string;
  revision: number;
}

export interface ProwlarrVerifyResponse {
  source: "prowlarr";
  status: ProwlarrVerificationStatus;
  configured: boolean;
  base_url: string | null;
  message_code: string | null;
  checked_at: string;
  state?: ProwlarrHealthState | null;
  message_zh?: string | null;
  reason_code?: string | null;
  reason_zh?: string | null;
  retry_after_seconds?: number | null;
}

export type TaskState =
  | "queued"
  | "submitting"
  | "submitted"
  | "downloading"
  | "available"
  | "needs_auth"
  | "failed"
  | "uncertain"
  | "cancelled";

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
  tmdb_season_id?: number | null;
  overview_available?: boolean;
}

export interface SeasonEpisodeMetadata {
  episode_number: number;
  name: string;
  overview: string | null;
  air_date: string | null;
  still_path: string | null;
  runtime: number | null;
  vote_average: number | null;
}

export interface SeasonDetailResponse {
  series_tmdb_id: number;
  tmdb_season_id: number | null;
  season_number: number;
  name: string;
  overview: string | null;
  overview_language: string | null;
  poster_path: string | null;
  air_date: string | null;
  episode_count: number;
  vote_average: number | null;
  source: "tmdb";
  fetched_at: string;
  cached: boolean;
  stale: boolean;
  data_version: number;
  episodes: SeasonEpisodeMetadata[];
  warnings: string[];
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
  sources?: string[];
  source_count?: number;
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

export interface ResourceSearchResponse {
  task_id: string;
  tmdb_id: number;
  media_type: "movie" | "tv";
  season_number: number | null;
  status: "queued" | "running" | "ready" | "failed" | "timeout";
  snapshot_revision: string | null;
  query_plan_version: string;
  selected_season: number | null;
  sources: string[];
  warnings: string[];
  cache_age_seconds: number | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
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
  release: string;
  push_supported: boolean;
  push_capabilities?: {
    magnet: boolean;
    share: boolean;
  };
  inspection_supported?: boolean;
  inspection_auto_start_enabled?: boolean;
  organization_plan_enabled?: boolean;
  organization_execution_enabled?: boolean;
  organization_execution_supported?: boolean;
  organization_write_enabled?: boolean;
  organization_write_contract_verified?: boolean;
  permanent_delete_enabled?: boolean;
  permanent_delete_contract_verified?: boolean;
  strm_capabilities?: {
    full: boolean;
    incremental: boolean;
    cleanup: boolean;
    cleanup_capability?: CapabilityAvailability;
    playback: boolean;
    playback_contract_verified: boolean;
  };
  organization_capabilities?: {
    empty_directory_cleanup?: CapabilityAvailability;
  };
}

export interface CapabilityAvailability {
  enabled: boolean;
  reason_code?: string | null;
  reason_zh: string;
  settings_section?: "overview" | "organization" | null;
}

export type LogLevel = "DEBUG" | "ERROR" | "WARNING" | "INFO";
export type LogCategory = "system" | "security" | "search" | "pansou" | "cache" | "inspection" | "p115" | "task" | "organize" | "strm" | "library" | "agent" | "settings" | "subscription" | "quality" | "notification";

export interface SettingsOverviewResponse {
  release: string;
  uptime_seconds: number;
  database_size_bytes: number;
  capabilities: {
    inspection: boolean;
    magnet: boolean;
    share: boolean;
    organization_plan: boolean;
    organization_execution: boolean;
    organization_write: boolean;
    permanent_delete: boolean;
    strm_full: boolean;
    strm_incremental: boolean;
    strm_cleanup: boolean;
    strm_playback: boolean;
    organization_empty_directory_cleanup: boolean;
  };
  capability_statuses?: Record<string, CapabilityStatusResponse>;
  capability_details?: Record<string, CapabilityAvailability>;
}

export type CapabilityState = "unconfigured" | "configured" | "contract_verified" | "runtime_healthy" | "recent_success";

export interface CapabilityStatusResponse {
  state: CapabilityState;
  state_zh: string;
  last_success_at: string | null;
}

export interface LibraryScanSummary {
  run_id: string;
  state: "queued" | "running" | "completed" | "failed" | "cancelled";
  complete: boolean;
  snapshot_revision: number | null;
  pages_read: number;
  items_seen: number;
  added_count: number;
  changed_count: number;
  removed_count: number;
  attempts: number;
  state_message_zh: string;
  error_code: string | null;
  error_message_zh: string | null;
  cancel_requested: boolean;
}

export interface MediaLibraryResponse {
  library_id: string;
  name: string;
  root_directory_id: string;
  enabled: boolean;
  scope_verified: boolean;
  revision: number;
  latest_scan: LibraryScanSummary | null;
}

export interface MediaLibraryListResponse {
  items: MediaLibraryResponse[];
  next_cursor: number | null;
}

export interface MediaEntryResponse {
  media_id: string;
  library_id: string;
  scan_run_id: string;
  object_type: "file";
  object_id: string;
  parent_id: string | null;
  name: string;
  size_bytes: number | null;
  modified_at: string | null;
  state: "indexed";
}

export interface MediaEntryListResponse {
  items: MediaEntryResponse[];
  next_cursor: number | null;
}

export interface StrmManifestItemResponse {
  manifest_id: string;
  library_id: string;
  cloud_file_id: string;
  cloud_relative_path: string;
  local_relative_path: string;
  status: "pending" | "verified" | "retired";
  source_version: number;
}

export interface StrmManifestListResponse {
  items: StrmManifestItemResponse[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface StrmGenerationResponse {
  operation_id: string;
  library_id: string;
  scan_run_id: string;
  generated: number;
  unchanged: number;
  skipped: number;
  failed: number;
  retired: number;
}

export interface StrmOperationResponse {
  operation_id: string;
  library_id: string;
  source_scan_run_id: string;
  workflow_id: string | null;
  kind: "full" | "incremental" | "cleanup";
  status: "queued" | "running" | "succeeded" | "failed" | "timeout" | "cancelled";
  generated: number;
  unchanged: number;
  skipped: number;
  failed: number;
  retired: number;
  error_code: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface StrmOperationListResponse {
  items: StrmOperationResponse[];
  next_cursor: string | null;
}

export interface StrmCleanupPlanResponse {
  plan_id: string;
  library_id: string;
  source_scan_run_id: string;
  source_snapshot_revision: number;
  plan_hash: string;
  status: "needs_review" | "invalidated" | "applied";
  revision: number;
  expires_at: string;
  candidate_count: number;
  executable_count: number;
  blocked_count: number;
}

export interface StrmCleanupPlanApplyResponse {
  plan: StrmCleanupPlanResponse;
  retired: number;
}

export interface EmptyDirectoryCleanupCandidateResponse {
  directory_id: string;
  parent_id: string;
  name: string;
  path: string;
  state: "ready" | "blocked";
}

export interface EmptyDirectoryCleanupPlanResponse {
  plan_id: string;
  library_id: string;
  source_scan_run_id: string;
  source_snapshot_revision: number;
  plan_hash: string;
  status: "needs_review" | "applying" | "invalidated" | "applied";
  revision: number;
  expires_at: string;
  candidate_count: number;
  executable_count: number;
  blocked_count: number;
  candidates: EmptyDirectoryCleanupCandidateResponse[];
}

export interface EmptyDirectoryCleanupPlanApplyResponse {
  plan: EmptyDirectoryCleanupPlanResponse;
  deleted: number;
}

export interface SmallFileCleanupCandidateResponse {
  file_id: string;
  parent_id: string;
  name: string;
  size_bytes: number;
}

export interface SmallFileCleanupPreviewResponse {
  library_id: string;
  source_scan_run_id: string;
  snapshot_revision: number;
  threshold_bytes: number;
  candidate_count: number;
  candidates: SmallFileCleanupCandidateResponse[];
}

export interface SmallFileCleanupApplyResponse {
  deleted: number;
  failed: number;
  total: number;
}

export type OrganizationOperationStatus = "planned" | "organizing" | "organized" | "failed" | "uncertain" | "cancelled";

export interface OrganizationOperationResponse {
  operation_id: string;
  plan_id: string;
  status: OrganizationOperationStatus;
  revision: number;
  attempts: number;
  error_code: string | null;
  cancel_requested: boolean;
  workflow_id: string | null;
}

export interface OrganizationOperationBatchResult {
  plan_id: string;
  operation_id: string | null;
  status: OrganizationOperationStatus | "rejected";
  revision: number | null;
  attempts: number | null;
  error_code: string | null;
  message: string;
}

export interface OrganizationOperationBatchResponse {
  items: OrganizationOperationBatchResult[];
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

export interface InspectionSettingsResponse {
  auto_start_enabled: boolean;
  revision: number;
}

export interface OrganizationSettingsResponse {
  schedule_enabled: boolean;
  auto_execute_enabled: boolean;
  scan_interval_minutes: number;
  source_directory_ids: string[];
  source_directory_labels?: string[];
  target_directory_id: string | null;
  target_directory_label?: string | null;
  push_directory_id: string | null;
  push_directory_label?: string | null;
  video_extensions: string[];
  metadata_extensions: string[];
  rename_enabled: boolean;
  media_probe_enabled: boolean;
  ai_identification_enabled: boolean;
  small_file_threshold_mb: number;
  cleanup_empty_directories: boolean;
  strm_linkage_enabled: boolean;
  operation_delay_seconds: number;
  include_children_category: boolean;
  include_concert_category: boolean;
  region_grouping_enabled: boolean;
  year_grouping_enabled: boolean;
  prefer_remux: boolean;
  prefer_resolution: boolean;
  prefer_dolby: boolean;
  conflict_mode: 0 | 1 | 2;
  multi_version_enabled: boolean;
  revision: number;
}

export interface PatchOrganizationSettingsRequest {
  schedule_enabled?: boolean;
  auto_execute_enabled?: boolean;
  scan_interval_minutes?: number;
  source_directory_ids?: string[];
  source_directory_labels?: string[];
  target_directory_id?: string | null;
  target_directory_label?: string | null;
  push_directory_id?: string | null;
  push_directory_label?: string | null;
  video_extensions?: string[];
  metadata_extensions?: string[];
  rename_enabled?: boolean;
  media_probe_enabled?: boolean;
  ai_identification_enabled?: boolean;
  small_file_threshold_mb?: number;
  cleanup_empty_directories?: boolean;
  strm_linkage_enabled?: boolean;
  operation_delay_seconds?: number;
  include_children_category?: boolean;
  include_concert_category?: boolean;
  region_grouping_enabled?: boolean;
  year_grouping_enabled?: boolean;
  prefer_remux?: boolean;
  prefer_resolution?: boolean;
  prefer_dolby?: boolean;
  conflict_mode?: 0 | 1 | 2;
  multi_version_enabled?: boolean;
  revision: number;
}

export interface OrganizationScheduleActionResponse {
  action: "run_now" | "stop";
  queued: boolean;
  schedule_enabled: boolean;
  message_zh: string;
  run_id: string | null;
}

export type OrganizationResultStatus = "unknown" | "success" | "skipped" | "deleted" | "replace" | "failed";

export interface OrganizationBlockedDetail {
  source_directory_id: string | null;
  phase: "credentials" | "directory_read" | "scan" | "tmdb" | "plan" | "execution";
  error_code: string;
  message_zh: string;
  next_step_zh: string;
}

export interface OrganizationResultItem {
  title: string;
  tmdb_id: number | null;
  target: string | null;
  status: "queued" | "organizing" | "success" | "failed" | "uncertain" | "needs_review" | "skipped";
  error_code: string | null;
}

export interface OrganizationAutomationResultResponse {
  status: OrganizationResultStatus;
  available_statuses: OrganizationResultStatus[];
  source_count: number;
  scanned_count: number;
  plan_count: number;
  queued_count: number;
  blocked_count: number;
  blocked_details: OrganizationBlockedDetail[];
  items: OrganizationResultItem[];
  finished_at: string | null;
  run_id: string | null;
  cleaned_small_files: number;
  cleaned_empty_dirs: number;
}

export interface PatchInspectionSettingsRequest {
  auto_start_enabled: boolean;
  revision: number;
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
    source: "managed" | "file";
    configured: boolean;
    structure_valid: boolean;
  };
  target_configured: boolean;
  max_concurrency: number;
}

export interface CredentialSourceResponse {
  configured: boolean;
  source: "managed" | "environment";
  last_updated_at: string | null;
}

export interface P115CredentialStatus {
  configured: boolean;
  source: "managed" | "file";
  last_updated_at: string | null;
  structure_valid: boolean;
  ready: boolean;
}

export interface CredentialSettingsResponse {
  revision: number;
  tmdb: CredentialSourceResponse;
  p115_cookie: P115CredentialStatus;
}

export interface P115ValidationResponse {
  status: "ready" | "needs_auth" | "unavailable";
  checked_at: string;
}

export interface P115DirectoryItem {
  id: string;
  name: string;
}

export interface P115DirectoryListResponse {
  root_id: string;
  parent_id: string;
  items: P115DirectoryItem[];
  has_more: boolean;
  next_page: number | null;
}

export interface P115LoginDevice {
  id: string;
  name: string;
  device_code: string;
  active: boolean;
  created_at: string;
  last_used_at: string | null;
}

export interface P115LoginDeviceListResponse {
  items: P115LoginDevice[];
}

export interface P115QrcodeCreateResponse {
  session_id: string;
  image_data_url: string;
  expires_at: string;
  status: "waiting";
}

export interface P115QrcodeStatusResponse {
  session_id: string;
  status: "waiting" | "scanned" | "ready" | "expired";
  device: P115LoginDevice | null;
}

export interface P115CheckInSettingsResponse {
  enabled: boolean;
  check_in_time: string;
}

export interface P115CheckInStatusResponse {
  enabled: boolean;
  is_sign_today: boolean;
  continuous_day: number;
  points_num: string;
  error_code: string | null;
}

export interface LogEntry {
  id: number;
  timestamp: string;
  level: LogLevel;
  category: LogCategory;
  message: string;
  event_code?: string;
  title_zh?: string;
  message_zh?: string;
  suggestion_zh?: string | null;
  status?: string | null;
  request_id?: string | null;
  correlation_id?: string | null;
  actor_type?: string | null;
  actor_id?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  task_id?: string | null;
  duration_ms?: number | null;
  error_code?: string | null;
}

export interface LogsResponse {
  items: LogEntry[];
  next_cursor: number | null;
}

export type OrganizationPlanStatus = "needs_review" | "planned" | "invalidated" | "ignored";

export interface OrganizationPlanSummary {
  plan_id: string;
  plan_hash: string;
  status: OrganizationPlanStatus;
  revision: number;
  expires_at: string;
  source_count: number;
  action_count: number;
  precondition_count: number;
  executable_action_count: number;
  review_action_count: number;
  can_execute: boolean;
  execution_blockers?: OrganizationExecutionBlocker[];
  /** REQ-012/REQ-003: 高风险整理计划需要 Web 人工批准 */
  requires_web_approval: boolean;
  high_risk_action_threshold: number;
  alias: string | null;
  candidates: OrganizationPlanCandidate[];
  /** 计划涉及的文件名(后端最多返回 8 个);旧后端可能不返回该字段 */
  source_names?: string[];
}

export type OrganizationExecutionBlockerKind =
  | "expired"
  | "status"
  | "prerequisite"
  | "snapshot_changed"
  | "review_only";

export interface OrganizationExecutionBlocker {
  kind: OrganizationExecutionBlockerKind;
  code: string;
  message_zh: string;
  next_step_zh: string;
}

export interface OrganizationPlanCandidate {
  source_object_id: string;
  tmdb_id: number;
  title: string;
  media_type: "movie" | "tv";
  release_year: number | null;
}

export interface OrganizationPlanListResponse {
  items: OrganizationPlanSummary[];
  next_cursor: number | null;
}

export interface OrganizationHistoryItem {
  id: string;
  operation_id: string;
  plan_id: string;
  source_object_id: string;
  source_directory_id: string;
  target_directory_id: string;
  tmdb_id: number | null;
  title: string;
  media_type: "movie" | "tv" | null;
  source_name: string;
  target_path: string;
  status: "organized" | "failed" | "uncertain";
  error_code: string | null;
  completed_at: string;
}

export interface OrganizationHistoryListResponse {
  items: OrganizationHistoryItem[];
  next_cursor: number | null;
}

export interface TaskResponse {
  id: string;
  resource_id: string | null;
  workflow_id: string | null;
  target_directory_id: string | null;
  action: "offline_download" | "save_share";
  state: TaskState;
  attempts: number;
  remote_ref: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  state_zh: string;
  state_reason_zh: string | null;
}

export type WorkflowEvidenceStatus =
  | "discovered"
  | "submitted"
  | "downloading"
  | "available"
  | "failed"
  | "uncertain";
export type WorkflowEvidenceSource =
  | "resource_record"
  | "submission_receipt"
  | "readonly_reconciliation";

export interface WorkflowEvidenceResponse {
  id: string;
  workflow_id: string | null;
  task_id: string | null;
  stage: WorkflowStageName | null;
  evidence_type: string;
  source: WorkflowEvidenceSource;
  subject_id: string;
  status: WorkflowEvidenceStatus;
  verified: boolean;
  observed_at: string;
}

export interface WorkflowEvidenceListResponse {
  items: WorkflowEvidenceResponse[];
}

export interface TaskReconciliationResponse {
  task: TaskResponse;
  evidence: WorkflowEvidenceResponse;
}

export type WorkflowStatus = "in_progress" | "waiting_user_confirmation" | "waiting_external" | "partial" | "completed" | "cancelled" | "failed" | "result_pending_confirmation";
export type WorkflowStageName = "discovery" | "inspection" | "approval" | "push" | "availability" | "organization" | "strm";
export type WorkflowStageStatus = "pending" | "running" | "waiting_confirmation" | "waiting_external" | "succeeded" | "skipped" | "failed" | "uncertain" | "cancelled";

export interface WorkflowStageResponse {
  id: string;
  stage: WorkflowStageName;
  status: WorkflowStageStatus;
  status_zh: string;
  reason: string | null;
  reason_zh: string | null;
  error_code: string | null;
  child_type: string | null;
  child_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
}

export interface WorkflowResponse {
  id: string;
  correlation_id: string;
  media_type: "movie" | "tv" | null;
  tmdb_id: number | null;
  subscription_id: string | null;
  status: WorkflowStatus;
  status_zh: string;
  state_reason: string | null;
  state_reason_zh: string | null;
  created_at: string;
  updated_at: string;
  stages: WorkflowStageResponse[];
}

export interface WorkflowListResponse {
  items: WorkflowResponse[];
  page: number;
  page_size: number;
  total: number;
}

export type NotificationSeverity = "info" | "warning" | "error" | "security";

export interface NotificationResponse {
  id: string;
  event_code: string;
  severity: NotificationSeverity;
  title_zh: string;
  message_zh: string;
  action_type: string | null;
  action_id: string | null;
  aggregate_count: number;
  read_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationListResponse {
  items: NotificationResponse[];
  unread_count: number;
}

export interface NotificationPreferenceResponse {
  enabled: boolean;
  muted_event_codes: string[];
  quiet_hours_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
  quiet_hours_timezone: string;
  error_bypass_quiet_hours: boolean;
  revision: number;
}

export interface PwaDevice {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string;
  revoked_at: string | null;
  status: "active" | "revoked";
}
export type SubscriptionMode = "remind" | "confirm" | "auto";
export type SubscriptionStatus =
  | "active"
  | "matched"
  | "paused"
  | "cancelled"
  | "completed"
  | "no_match";

export interface SubscriptionResponse {
  id: string;
  tmdb_id: number;
  media_type: "movie" | "tv";
  season_number: number | null;
  episode_start: number | null;
  episode_end: number | null;
  mode: SubscriptionMode;
  status: SubscriptionStatus;
  quality_profile_id: string | null;
  next_check_at: string | null;
  last_checked_at: string | null;
  last_match_count: number;
  last_error_code: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface SubscriptionCreateRequest {
  tmdb_id: number;
  media_type: "movie" | "tv";
  season_number?: number | null;
  episode_start?: number | null;
  episode_end?: number | null;
  mode?: SubscriptionMode;
}

export interface SubscriptionMutationRequest {
  revision: number;
}

export interface SubscriptionCheckResponse {
  subscription_id: string;
  status: SubscriptionStatus;
  next_check_at: string | null;
  new_matches: number;
}

export interface SubscriptionResourceObservationResponse {
  resource_id: string;
  captured_at: string;
  source: string;
  name: string;
  size_bytes: number | null;
}
