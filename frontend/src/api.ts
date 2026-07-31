import type {
  HealthResponse,
  InspectionSettingsResponse,
  OrganizationSettingsResponse,
  OrganizationScheduleActionResponse,
  HomeCatalogResponse,
  InspectionBatchResponse,
  LogCategory,
  LoggingSettingsResponse,
  ContentPolicyResponse,
  CredentialSettingsResponse,
  PatchContentPolicyRequest,
  LogsResponse,
  MovieCollectionResponse,
  MovieMetadata,
  PatchLoggingSettingsRequest,
  PatchInspectionSettingsRequest,
  PatchOrganizationSettingsRequest,
  P115SettingsResponse,
  P115ValidationResponse,
  SearchRequest,
  SearchResponse,
  SeasonDetailResponse,
  ResourcePageResponse,
  ResourceSearchResponse,
  ResourceQuality,
  ResourceSort,
  SettingsOverviewResponse,
  TaskResponse,
  WorkflowListResponse,
  WorkflowResponse,
  WorkflowStageName,
  WorkflowStageStatus,
  NotificationListResponse,
  NotificationResponse,
  NotificationPreferenceResponse,
  OrganizationPlanListResponse,
  OrganizationPlanSummary,
  OrganizationOperationResponse,
  MediaLibraryListResponse,
  MediaLibraryResponse,
  MediaEntryListResponse,
  StrmManifestListResponse,
  StrmGenerationResponse,
  PwaDevice,
  BackupConfigurationExportResponse,
  BackupConfigurationImportRequest,
  BackupConfigurationImportResponse,
  BackupListResponse,
  BackupDeleteResponse,
} from "./types";
import { describeUiError, type UiErrorAction } from "./errorCatalog";

export interface ApiFieldError {
  fieldId: string;
  message: string;
}

export function browserIsOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

export class ApiError extends Error {
  readonly title: string;
  readonly suggestion: string;
  readonly retryable: boolean;
  readonly action: UiErrorAction;
  readonly fieldErrors: ApiFieldError[];
  readonly requestId: string | null;
  readonly correlationId: string | null;

  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    details: Partial<Pick<ApiError, "title" | "suggestion" | "retryable" | "action" | "fieldErrors" | "requestId" | "correlationId">> = {},
  ) {
    super(message);
    this.title = details.title ?? "操作暂时无法完成";
    this.suggestion = details.suggestion ?? "请检查当前状态后再试。";
    this.retryable = details.retryable ?? false;
    this.action = details.action ?? null;
    this.fieldErrors = details.fieldErrors ?? [];
    this.requestId = details.requestId ?? null;
    this.correlationId = details.correlationId ?? null;
  }
}

export function focusFirstFieldError(exception: unknown): void {
  if (!(exception instanceof ApiError) || !exception.fieldErrors.length || typeof document === "undefined") return;
  const fieldId = exception.fieldErrors[0]?.fieldId;
  if (!fieldId) return;
  const byId = document.getElementById(fieldId);
  if (byId instanceof HTMLElement) {
    byId.focus();
    return;
  }
  const byName = Array.from(document.querySelectorAll<HTMLElement>("[name]"))
    .find((element) => element.getAttribute("name") === fieldId);
  byName?.focus();
}

export class ApiClient {
  private csrfToken: string | null = null;

  async login(password: string): Promise<void> {
    const response = await this.request<{ csrf_token: string }>(
      "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ password }) },
    );
    this.csrfToken = response.csrf_token;
  }

  async me(): Promise<void> {
    const response = await this.request<{ csrf_token: string | null }>(
      "/api/v1/auth/me",
    );
    this.csrfToken = response.csrf_token;
  }

  async health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("/api/v1/health");
  }

  async settingsOverview(): Promise<SettingsOverviewResponse> {
    return this.request<SettingsOverviewResponse>("/api/v1/settings/overview");
  }

  async exportConfiguration(): Promise<BackupConfigurationExportResponse> {
    return this.request<BackupConfigurationExportResponse>("/api/v1/backups/configuration");
  }

  async importConfiguration(
    configuration: BackupConfigurationImportRequest,
  ): Promise<BackupConfigurationImportResponse> {
    return this.request<BackupConfigurationImportResponse>("/api/v1/backups/configuration/import", {
      method: "POST",
      body: JSON.stringify(configuration),
    });
  }

  async backups(): Promise<BackupListResponse> {
    return this.request<BackupListResponse>("/api/v1/backups");
  }

  async deleteBackup(backupId: string): Promise<BackupDeleteResponse> {
    return this.request<BackupDeleteResponse>(`/api/v1/backups/${encodeURIComponent(backupId)}`, {
      method: "DELETE",
      body: JSON.stringify({ confirmed: true }),
    });
  }

  async loggingSettings(): Promise<LoggingSettingsResponse> {
    return this.request<LoggingSettingsResponse>("/api/v1/settings/logging");
  }

  async updateLoggingSettings(settings: PatchLoggingSettingsRequest): Promise<LoggingSettingsResponse> {
    return this.request<LoggingSettingsResponse>("/api/v1/settings/logging", {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  }

  async contentPolicy(): Promise<ContentPolicyResponse> {
    return this.request<ContentPolicyResponse>("/api/v1/settings/content-policy");
  }

  async updateContentPolicy(settings: PatchContentPolicyRequest): Promise<ContentPolicyResponse> {
    return this.request<ContentPolicyResponse>("/api/v1/settings/content-policy", {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  }

  async inspectionSettings(): Promise<InspectionSettingsResponse> {
    return this.request<InspectionSettingsResponse>("/api/v1/settings/inspection");
  }

  async updateInspectionSettings(settings: PatchInspectionSettingsRequest): Promise<InspectionSettingsResponse> {
    return this.request<InspectionSettingsResponse>("/api/v1/settings/inspection", {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  }

  async p115Settings(): Promise<P115SettingsResponse> {
    return this.request<P115SettingsResponse>("/api/v1/settings/p115");
  }

  async credentialSettings(): Promise<CredentialSettingsResponse> {
    return this.request<CredentialSettingsResponse>("/api/v1/settings/credentials");
  }

  async updateTmdbCredential(value: string, revision: number): Promise<CredentialSettingsResponse> {
    return this.request<CredentialSettingsResponse>("/api/v1/settings/credentials/tmdb", {
      method: "PUT",
      body: JSON.stringify({ value, revision }),
    });
  }

  async resetTmdbCredential(revision: number): Promise<CredentialSettingsResponse> {
    return this.request<CredentialSettingsResponse>("/api/v1/settings/credentials/tmdb/reset", {
      method: "POST",
      body: JSON.stringify({ revision }),
    });
  }

  async updateP115Cookie(value: string, revision: number): Promise<CredentialSettingsResponse> {
    return this.request<CredentialSettingsResponse>("/api/v1/settings/credentials/p115-cookie", {
      method: "PUT",
      body: JSON.stringify({ value, revision }),
    });
  }

  async resetP115Cookie(revision: number): Promise<CredentialSettingsResponse> {
    return this.request<CredentialSettingsResponse>("/api/v1/settings/credentials/p115-cookie/reset", {
      method: "POST",
      body: JSON.stringify({ revision }),
    });
  }

  async validateP115Cookie(): Promise<P115ValidationResponse> {
    return this.request<P115ValidationResponse>("/api/v1/settings/p115/validate", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async logs(filters: {
    category?: LogCategory;
    level?: import("./types").LogLevel;
    status?: string;
    eventCode?: string;
    requestId?: string;
    correlationId?: string;
    taskId?: string;
    actorType?: string;
    actorId?: string;
    resourceType?: string;
    resourceId?: string;
    cursor?: number;
    limit?: number;
  }): Promise<LogsResponse> {
    const params = new URLSearchParams({ limit: String(filters.limit ?? 20) });
    if (filters.cursor !== undefined) params.set("cursor", String(filters.cursor));
    if (filters.category) params.set("category", filters.category);
    if (filters.level) params.set("level", filters.level);
    if (filters.status) params.set("status", filters.status);
    if (filters.eventCode) params.set("event_code", filters.eventCode);
    if (filters.requestId) params.set("request_id", filters.requestId);
    if (filters.correlationId) params.set("correlation_id", filters.correlationId);
    if (filters.taskId) params.set("task_id", filters.taskId);
    if (filters.actorType) params.set("actor_type", filters.actorType);
    if (filters.actorId) params.set("actor_id", filters.actorId);
    if (filters.resourceType) params.set("resource_type", filters.resourceType);
    if (filters.resourceId) params.set("resource_id", filters.resourceId);
    return this.request<LogsResponse>(`/api/v1/logs?${params}`);
  }

  async organizationSettings(): Promise<OrganizationSettingsResponse> {
    return this.request<OrganizationSettingsResponse>("/api/v1/settings/organization");
  }

  async updateOrganizationSettings(settings: PatchOrganizationSettingsRequest): Promise<OrganizationSettingsResponse> {
    return this.request<OrganizationSettingsResponse>("/api/v1/settings/organization", {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  }

  async runOrganizationNow(): Promise<OrganizationScheduleActionResponse> {
    return this.request<OrganizationScheduleActionResponse>("/api/v1/settings/organization/run-now", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async stopOrganization(): Promise<OrganizationScheduleActionResponse> {
    return this.request<OrganizationScheduleActionResponse>("/api/v1/settings/organization/stop", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async p115Directories(directoryId?: string, page = 1): Promise<import("./types").P115DirectoryListResponse> {
    const params = new URLSearchParams({ page: String(page) });
    if (directoryId) params.set("directory_id", directoryId);
    return this.request<import("./types").P115DirectoryListResponse>(`/api/v1/settings/p115/directories?${params}`);
  }

  async p115Devices(): Promise<import("./types").P115LoginDeviceListResponse> {
    return this.request<import("./types").P115LoginDeviceListResponse>("/api/v1/settings/p115/devices");
  }

  async createP115Qrcode(deviceCode: string, deviceName = "这台电脑"): Promise<import("./types").P115QrcodeCreateResponse> {
    return this.request<import("./types").P115QrcodeCreateResponse>("/api/v1/settings/p115/qrcode", {
      method: "POST",
      body: JSON.stringify({ device_code: deviceCode, device_name: deviceName }),
    });
  }

  async pollP115Qrcode(sessionId: string): Promise<import("./types").P115QrcodeStatusResponse> {
    return this.request<import("./types").P115QrcodeStatusResponse>(`/api/v1/settings/p115/qrcode/${encodeURIComponent(sessionId)}`);
  }

  async activateP115Device(deviceId: string, revision: number): Promise<import("./types").P115LoginDeviceListResponse> {
    return this.request<import("./types").P115LoginDeviceListResponse>(`/api/v1/settings/p115/devices/${encodeURIComponent(deviceId)}/activate`, {
      method: "POST",
      body: JSON.stringify({ revision }),
    });
  }

  async revokeP115Device(deviceId: string): Promise<void> {
    await this.request<void>(`/api/v1/settings/p115/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
  }

  async search(
    tmdbId: number,
    mediaType: "movie" | "tv" = "movie",
    refresh = false,
    seasonNumber?: number,
  ): Promise<SearchResponse> {
    const body: SearchRequest = { tmdb_id: tmdbId, media_type: mediaType, refresh };
    if (mediaType === "tv" && seasonNumber !== undefined) body.season_number = seasonNumber;
    return this.request<SearchResponse>("/api/v1/search", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async mediaMetadata(
    mediaType: "movie" | "tv",
    tmdbId: number,
    signal?: AbortSignal,
  ): Promise<MovieMetadata> {
    return this.request<MovieMetadata>(`/api/v1/media/${mediaType}/${tmdbId}`, { signal });
  }

  async recordMediaDetailMetric(
    mediaType: "movie" | "tv",
    tmdbId: number,
    metric: {
      stage: "detail_framework" | "metadata_summary" | "metadata_complete" | "metadata_failed" | "resource_first_batch" | "resource_complete" | "resource_failed" | "late_response" | "request_cancelled";
      status: "success" | "failed" | "discarded" | "cancelled";
      duration_ms: number;
      cached?: boolean;
      season_number?: number | null;
      error_code?: string;
    },
  ): Promise<void> {
    await this.request<void>(`/api/v1/media/${mediaType}/${tmdbId}/performance`, {
      method: "POST",
      body: JSON.stringify({ cached: false, ...metric }),
    });
  }

  async seasonMetadata(
    tmdbId: number,
    seasonNumber: number,
    options: { refresh?: boolean; language?: string; fallbackLanguage?: string } = {},
    signal?: AbortSignal,
  ): Promise<SeasonDetailResponse> {
    const params = new URLSearchParams({
      language: options.language ?? "zh-CN",
      fallback_language: options.fallbackLanguage ?? "en-US",
    });
    if (options.refresh) params.set("refresh", "true");
    return this.request<SeasonDetailResponse>(
      `/api/v1/media/tv/${tmdbId}/seasons/${seasonNumber}?${params}`,
      { signal },
    );
  }

  async resources(
    mediaType: "movie" | "tv",
    tmdbId: number,
    filters: {
      seasonNumber?: number | null;
      kind?: "magnet" | "115_share";
      quality?: ResourceQuality;
      query?: string;
      sort: ResourceSort;
      page: number;
      pageSize: 25 | 50 | 100;
    },
    signal?: AbortSignal,
  ): Promise<ResourcePageResponse> {
    const params = new URLSearchParams({
      sort: filters.sort,
      page: String(filters.page),
      page_size: String(filters.pageSize),
    });
    if (filters.seasonNumber !== undefined && filters.seasonNumber !== null) params.set("season_number", String(filters.seasonNumber));
    if (filters.kind) params.set("kind", filters.kind);
    if (filters.quality) params.set("quality", filters.quality);
    if (filters.query?.trim()) params.set("query", filters.query.trim());
    return this.request<ResourcePageResponse>(`/api/v1/media/${mediaType}/${tmdbId}/resources?${params}`, { signal });
  }

  async startResourceSearch(
    mediaType: "movie" | "tv",
    tmdbId: number,
    options: { seasonNumber?: number | null; refresh?: boolean } = {},
    signal?: AbortSignal,
  ): Promise<ResourceSearchResponse> {
    return this.request<ResourceSearchResponse>(`/api/v1/media/${mediaType}/${tmdbId}/resource-search`, {
      method: "POST",
      body: JSON.stringify({
        season_number: options.seasonNumber ?? null,
        refresh: options.refresh ?? false,
      }),
      signal,
    });
  }

  async resourceSearch(taskId: string, signal?: AbortSignal): Promise<ResourceSearchResponse> {
    return this.request<ResourceSearchResponse>(`/api/v1/resource-search/${encodeURIComponent(taskId)}`, { signal });
  }

  async inspectResources(resourceIds: string[], workflowId?: string | null): Promise<InspectionBatchResponse> {
    return this.request<InspectionBatchResponse>("/api/v1/resources/inspect", {
      method: "POST",
      body: JSON.stringify({ resource_ids: resourceIds, ...(workflowId ? { workflow_id: workflowId } : {}) }),
    });
  }

  async getInspection(batchId: string, signal?: AbortSignal): Promise<InspectionBatchResponse> {
    return this.request<InspectionBatchResponse>(`/api/v1/resources/inspect/${encodeURIComponent(batchId)}`, { signal });
  }

  async popularMovies(page = 1): Promise<MovieCollectionResponse> {
    return this.request<MovieCollectionResponse>(`/api/v1/movies/popular?page=${page}`);
  }

  async homeCatalog(): Promise<HomeCatalogResponse> {
    return this.request<HomeCatalogResponse>("/api/v1/movies/home");
  }

  async discoverMedia(mediaType: "movie" | "tv", filters: {
    genreId?: number;
    year?: number;
    sort: "popular" | "rating" | "release";
    page?: number;
  }): Promise<MovieCollectionResponse> {
    const params = new URLSearchParams({
      media_type: mediaType,
      sort: filters.sort,
      page: String(filters.page ?? 1),
    });
    if (filters.genreId) params.set("genre_id", String(filters.genreId));
    if (filters.year) params.set("year", String(filters.year));
    return this.request<MovieCollectionResponse>(`/api/v1/media/discover?${params}`);
  }

  async searchMedia(query: string, page = 1): Promise<MovieCollectionResponse> {
    const params = new URLSearchParams({ query, page: String(page) });
    return this.request<MovieCollectionResponse>(`/api/v1/media/search?${params}`);
  }

  async createTask(resourceId: string, force = false, workflowId?: string | null, targetDirectoryId?: string | null): Promise<TaskResponse> {
    return this.request<TaskResponse>("/api/v1/tasks", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId, force, ...(workflowId ? { workflow_id: workflowId } : {}), ...(targetDirectoryId ? { target_directory_id: targetDirectoryId } : {}) }),
    });
  }

  async getTask(taskId: string): Promise<TaskResponse> {
    return this.request<TaskResponse>(`/api/v1/tasks/${taskId}`);
  }

  async workflows(filters: { status?: string; mediaType?: "movie" | "tv"; subscriptionId?: string; stage?: string; stageStatus?: string; page?: number; pageSize?: number } = {}): Promise<WorkflowListResponse> {
    const params = new URLSearchParams({ page: String(filters.page ?? 1), page_size: String(filters.pageSize ?? 20) });
    if (filters.status) params.set("status", filters.status);
    if (filters.mediaType) params.set("media_type", filters.mediaType);
    if (filters.subscriptionId) params.set("subscription_id", filters.subscriptionId);
    if (filters.stage) params.set("stage", filters.stage);
    if (filters.stageStatus) params.set("stage_status", filters.stageStatus);
    return this.request<WorkflowListResponse>(`/api/v1/workflows?${params}`);
  }

  async workflow(workflowId: string): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>(`/api/v1/workflows/${encodeURIComponent(workflowId)}`);
  }

  async createWorkflow(input: { mediaType: "movie" | "tv"; tmdbId: number }): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>("/api/v1/workflows", {
      method: "POST",
      body: JSON.stringify({ media_type: input.mediaType, tmdb_id: input.tmdbId }),
    });
  }

  async patchWorkflowStage(
    workflowId: string,
    stage: import("./types").WorkflowStageName,
    patch: { status: import("./types").WorkflowStageStatus; reason?: string; errorCode?: string; childType?: string; childId?: string },
  ): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>(`/api/v1/workflows/${encodeURIComponent(workflowId)}/stages/${stage}`, {
      method: "PATCH",
      body: JSON.stringify({
        status: patch.status,
        ...(patch.reason ? { reason: patch.reason } : {}),
        ...(patch.errorCode ? { error_code: patch.errorCode } : {}),
        ...(patch.childType ? { child_type: patch.childType } : {}),
        ...(patch.childId ? { child_id: patch.childId } : {}),
      }),
    });
  }

  async decideWorkflowApproval(workflowId: string, decision: "approve" | "reject", reason?: string): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>(`/api/v1/workflows/${encodeURIComponent(workflowId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ decision, ...(reason ? { reason } : {}) }),
    });
  }

  async cancelWorkflow(workflowId: string, reason?: string): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>(`/api/v1/workflows/${encodeURIComponent(workflowId)}/cancel`, {
      method: "POST",
      body: JSON.stringify(reason ? { reason } : {}),
    });
  }

  async notifications(unreadOnly = false, limit = 50): Promise<NotificationListResponse> {
    const params = new URLSearchParams({ unread_only: String(unreadOnly), limit: String(limit) });
    return this.request<NotificationListResponse>(`/api/v1/notifications?${params}`);
  }

  async markNotificationRead(notificationId: string): Promise<NotificationResponse> {
    return this.request<NotificationResponse>(`/api/v1/notifications/${encodeURIComponent(notificationId)}/read`, { method: "POST", body: "{}" });
  }

  async markAllNotificationsRead(): Promise<{ marked_count: number }> {
    return this.request<{ marked_count: number }>("/api/v1/notifications/read-all", { method: "POST", body: "{}" });
  }

  async notificationPreferences(): Promise<NotificationPreferenceResponse> {
    return this.request<NotificationPreferenceResponse>("/api/v1/notification-preferences");
  }

  async updateNotificationPreferences(patch: { enabled?: boolean; muted_event_codes?: string[]; quiet_hours_enabled?: boolean; quiet_hours_start?: string; quiet_hours_end?: string; quiet_hours_timezone?: string; error_bypass_quiet_hours?: boolean; revision: number }): Promise<NotificationPreferenceResponse> {
    return this.request<NotificationPreferenceResponse>("/api/v1/notification-preferences", { method: "PATCH", body: JSON.stringify(patch) });
  }

  async pwaDevices(): Promise<{ items: PwaDevice[] }> {
    return this.request<{ items: PwaDevice[] }>("/api/v1/pwa/devices");
  }

  async registerPwaDevice(name: string, subscription: PushSubscriptionJSON): Promise<PwaDevice> {
    return this.request<PwaDevice>("/api/v1/pwa/devices", {
      method: "POST",
      body: JSON.stringify({ name, subscription }),
    });
  }

  async revokePwaDevice(deviceId: string): Promise<PwaDevice> {
    return this.request<PwaDevice>(`/api/v1/pwa/devices/${encodeURIComponent(deviceId)}/revoke`, {
      method: "POST",
      body: "{}",
    });
  }

  async organizationPlans(filters: {
    status?: "needs_review" | "planned" | "invalidated" | "ignored";
    cursor?: number;
    limit?: number;
  } = {}): Promise<OrganizationPlanListResponse> {
    const params = new URLSearchParams({ limit: String(filters.limit ?? 20) });
    if (filters.status) params.set("status", filters.status);
    if (filters.cursor !== undefined) params.set("cursor", String(filters.cursor));
    return this.request<OrganizationPlanListResponse>(`/api/v1/organization-plans?${params}`);
  }

  async organizationPlan(planId: string): Promise<OrganizationPlanSummary> {
    return this.request<OrganizationPlanSummary>(`/api/v1/organization-plans/${encodeURIComponent(planId)}`);
  }

  async confirmOrganizationPlan(planId: string, expectedRevision: number): Promise<OrganizationPlanSummary> {
    return this.request<OrganizationPlanSummary>(`/api/v1/organization-plans/${encodeURIComponent(planId)}/confirm`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  }

  async ignoreOrganizationPlan(planId: string, expectedRevision: number): Promise<OrganizationPlanSummary> {
    return this.request<OrganizationPlanSummary>(`/api/v1/organization-plans/${encodeURIComponent(planId)}/ignore`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  }

  async aliasOrganizationPlan(planId: string, alias: string, expectedRevision: number): Promise<OrganizationPlanSummary> {
    return this.request<OrganizationPlanSummary>(`/api/v1/organization-plans/${encodeURIComponent(planId)}/alias`, {
      method: "POST",
      body: JSON.stringify({ alias, expected_revision: expectedRevision }),
    });
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const method = (init.method ?? "GET").toUpperCase();
    if (method !== "GET" && !browserIsOnline()) {
      throw new ApiError("当前处于离线状态，写操作已暂停。", 0, "offline_write_blocked", {
        title: "当前处于离线状态",
        suggestion: "恢复网络后再提交，系统不会在本地排队执行写操作。",
        retryable: true,
        action: "retry",
      });
    }
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (method !== "GET" && this.csrfToken) {
      headers.set("X-CSRF-Token", this.csrfToken);
    }
    if (method !== "GET" && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", createIdempotencyKey());
    }
    try {
      const requestInit: RequestInit = { ...init, headers, credentials: "same-origin" };
      if (init.method) requestInit.method = method;
      const response = await fetch(path, requestInit);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = body.error ?? body.detail;
        const code = typeof detail === "string" ? detail : detail?.code;
        const descriptor = describeUiError(code, response.status);
        const fieldErrors = parseFieldErrors(detail?.field_errors);
        const requestId = typeof detail?.request_id === "string" ? detail.request_id : response.headers.get("X-Request-ID");
        const correlationId = typeof detail?.correlation_id === "string" ? detail.correlation_id : response.headers.get("X-Correlation-ID");
        throw new ApiError(descriptor.message, response.status, descriptor.code, {
          ...descriptor,
          fieldErrors,
          requestId,
          correlationId,
        });
      }
      if (method === "GET" && isCacheableRead(path)) {
        writeReadCache(path, body);
      }
      return body as T;
    } catch (exception) {
      if (method === "GET" && isCacheableRead(path) && !(exception instanceof ApiError)) {
        const cached = readReadCache<T>(path);
        if (cached) {
          if (typeof window !== "undefined") {
            window.dispatchEvent(new CustomEvent("watch-assistant:offline-data", { detail: { cachedAt: cached.cachedAt } }));
          }
          return cached.data;
        }
      }
      throw exception;
    }
  }

  async queueOrganizationOperation(planId: string, expectedRevision: number): Promise<OrganizationOperationResponse> {
    return this.request<OrganizationOperationResponse>(`/api/v1/organization-plans/${encodeURIComponent(planId)}/operation`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: createIdempotencyKey(), confirm: true }),
    });
  }

  async organizationPlanOperation(planId: string): Promise<OrganizationOperationResponse> {
    return this.request<OrganizationOperationResponse>(`/api/v1/organization-plans/${encodeURIComponent(planId)}/operation`);
  }

  async organizationOperation(operationId: string): Promise<OrganizationOperationResponse> {
    return this.request<OrganizationOperationResponse>(`/api/v1/organization-operations/${encodeURIComponent(operationId)}`);
  }

  async libraries(cursor = 0, limit = 50): Promise<MediaLibraryListResponse> {
    return this.request<MediaLibraryListResponse>(`/api/v1/libraries?cursor=${cursor}&limit=${limit}`);
  }

  async configureLibrary(libraryId: string, payload: { name: string; root_directory_id: string; revision: number }): Promise<MediaLibraryResponse> {
    return this.request<MediaLibraryResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/configuration`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }

  async verifyLibraryScope(libraryId: string): Promise<{ library: MediaLibraryResponse; verified: boolean; enabled: boolean }> {
    return this.request<{ library: MediaLibraryResponse; verified: boolean; enabled: boolean }>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/verify-scope`, {
      method: "POST",
      body: "{}",
    });
  }

  async scanLibrary(libraryId: string): Promise<MediaLibraryResponse["latest_scan"]> {
    const response = await this.request<NonNullable<MediaLibraryResponse["latest_scan"]>>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/scan`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: createIdempotencyKey() }),
    });
    return response;
  }

  async createOrganizationPreview(
    libraryId: string,
    sourceScanRunId: string,
  ): Promise<OrganizationPlanSummary> {
    return this.request<OrganizationPlanSummary>(
      `/api/v1/libraries/${encodeURIComponent(libraryId)}/organization-preview`,
      {
        method: "POST",
        body: JSON.stringify({ source_scan_run_id: sourceScanRunId }),
      },
    );
  }

  async libraryMedia(libraryId: string, cursor = 0, limit = 50): Promise<MediaEntryListResponse> {
    return this.request<MediaEntryListResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/media?cursor=${cursor}&limit=${limit}`);
  }

  async strmManifest(libraryId: string, page = 1, pageSize = 50): Promise<StrmManifestListResponse> {
    return this.request<StrmManifestListResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/strm-manifest?page=${page}&page_size=${pageSize}`);
  }

  async generateStrm(libraryId: string, scanRunId: string): Promise<StrmGenerationResponse> {
    return this.request<StrmGenerationResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/strm-generation`, {
      method: "POST",
      body: JSON.stringify({ source_scan_run_id: scanRunId }),
    });
  }

  async incrementalStrm(libraryId: string, scanRunId: string): Promise<StrmGenerationResponse> {
    return this.request<StrmGenerationResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/strm-incremental`, {
      method: "POST",
      body: JSON.stringify({ source_scan_run_id: scanRunId }),
    });
  }

  async cleanupStrm(libraryId: string, scanRunId: string): Promise<StrmGenerationResponse> {
    return this.request<StrmGenerationResponse>(`/api/v1/libraries/${encodeURIComponent(libraryId)}/strm-cleanup`, {
      method: "POST",
      body: JSON.stringify({ source_scan_run_id: scanRunId }),
    });
  }
}

const READ_CACHE_PREFIX = "watch-assistant:readonly-cache:";
const READ_CACHE_TTL_MS = 15 * 60 * 1000;

function isCacheableRead(path: string): boolean {
  return /^\/api\/v1\/(health|tasks(?:[/?]|$)|notifications(?:[/?]|$)|workflows(?:[/?]|$))/.test(path);
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `wa-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function writeReadCache(path: string, data: unknown): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(`${READ_CACHE_PREFIX}${path}`, JSON.stringify({ cachedAt: new Date().toISOString(), data }));
  } catch {
    // Storage quotas and privacy mode must not affect online requests.
  }
}

function readReadCache<T>(path: string): { cachedAt: string; data: T } | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const value = localStorage.getItem(`${READ_CACHE_PREFIX}${path}`);
    if (!value) return null;
    const parsed = JSON.parse(value) as { cachedAt?: string; data?: T };
    if (!parsed.cachedAt || Date.now() - Date.parse(parsed.cachedAt) > READ_CACHE_TTL_MS || parsed.data === undefined) return null;
    return { cachedAt: parsed.cachedAt, data: parsed.data };
  } catch {
    return null;
  }
}

function parseFieldErrors(value: unknown): ApiFieldError[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): ApiFieldError[] => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const fieldId = typeof record.field_id === "string" ? record.field_id : typeof record.field === "string" ? record.field : "";
    const message = typeof record.message_zh === "string" ? record.message_zh : typeof record.message === "string" ? record.message : "";
    return fieldId && message ? [{ fieldId, message }] : [];
  });
}
