import type {
  HomeCatalogResponse,
  MovieCollectionResponse,
  SearchResponse,
  TaskResponse,
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

  async health(): Promise<{ status: string; push_supported: boolean }> {
    return this.request("/api/v1/health");
  }

  async search(
    tmdbId: number,
    mediaType: "movie" | "tv" = "movie",
    refresh = false,
  ): Promise<SearchResponse> {
    return this.request<SearchResponse>("/api/v1/search", {
      method: "POST",
      body: JSON.stringify({ tmdb_id: tmdbId, media_type: mediaType, refresh }),
    });
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
