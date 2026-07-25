import type {
  HealthResponse,
  HomeCatalogResponse,
  InspectionBatchResponse,
  LogLevel,
  LoggingSettingsResponse,
  LogsResponse,
  MovieCollectionResponse,
  P115SettingsResponse,
  SearchRequest,
  SearchResponse,
  SettingsOverviewResponse,
  TaskResponse,
  UpdateLoggingSettingsRequest,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
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

  async updateLoggingSettings(settings: UpdateLoggingSettingsRequest): Promise<LoggingSettingsResponse> {
    return this.request<LoggingSettingsResponse>("/api/v1/settings/logging", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  }

  async p115Settings(): Promise<P115SettingsResponse> {
    return this.request<P115SettingsResponse>("/api/v1/settings/p115");
  }

  async validateP115Cookie(): Promise<P115SettingsResponse> {
    return this.request<P115SettingsResponse>("/api/v1/settings/p115/validate", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async logs(filters: { level?: LogLevel; category?: string; page?: number; pageSize?: number }): Promise<LogsResponse> {
    const params = new URLSearchParams({
      page: String(filters.page ?? 1),
      page_size: String(filters.pageSize ?? 20),
    });
    if (filters.level) params.set("level", filters.level);
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
      throw new ApiError(body.detail ?? "请求失败", response.status);
    }
    return body as T;
  }
}
