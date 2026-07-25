import type {
  HealthResponse,
  HomeCatalogResponse,
  InspectionBatchResponse,
  LogCategory,
  LoggingSettingsResponse,
  LogsResponse,
  MovieCollectionResponse,
  PatchLoggingSettingsRequest,
  P115SettingsResponse,
  P115ValidationResponse,
  SearchRequest,
  SearchResponse,
  ResourcePageResponse,
  ResourceQuality,
  ResourceSort,
  SettingsOverviewResponse,
  TaskResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
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

  async loggingSettings(): Promise<LoggingSettingsResponse> {
    return this.request<LoggingSettingsResponse>("/api/v1/settings/logging");
  }

  async updateLoggingSettings(settings: PatchLoggingSettingsRequest): Promise<LoggingSettingsResponse> {
    return this.request<LoggingSettingsResponse>("/api/v1/settings/logging", {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  }

  async p115Settings(): Promise<P115SettingsResponse> {
    return this.request<P115SettingsResponse>("/api/v1/settings/p115");
  }

  async validateP115Cookie(): Promise<P115ValidationResponse> {
    return this.request<P115ValidationResponse>("/api/v1/settings/p115/validate", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async logs(filters: { category?: LogCategory; cursor?: number; limit?: number }): Promise<LogsResponse> {
    const params = new URLSearchParams({ limit: String(filters.limit ?? 20) });
    if (filters.cursor !== undefined) params.set("cursor", String(filters.cursor));
    if (filters.category) params.set("category", filters.category);
    return this.request<LogsResponse>(`/api/v1/logs?${params}`);
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

  async inspectResources(resourceIds: string[]): Promise<InspectionBatchResponse> {
    return this.request<InspectionBatchResponse>("/api/v1/resources/inspect", {
      method: "POST",
      body: JSON.stringify({ resource_ids: resourceIds }),
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

  async createTask(resourceId: string, force = false): Promise<TaskResponse> {
    return this.request<TaskResponse>("/api/v1/tasks", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId, force }),
    });
  }

  async getTask(taskId: string): Promise<TaskResponse> {
    return this.request<TaskResponse>(`/api/v1/tasks/${taskId}`);
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && this.csrfToken) {
      headers.set("X-CSRF-Token", this.csrfToken);
    }
    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail;
      const message = typeof detail === "string" ? detail : detail?.message ?? "请求失败";
      const code = typeof detail === "string" ? detail : detail?.code;
      throw new ApiError(message, response.status, code);
    }
    return body as T;
  }
}
