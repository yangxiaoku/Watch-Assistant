import type { SearchResponse, TaskResponse } from "./types";

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
    await this.request("/api/v1/auth/me");
  }

  async health(): Promise<{ status: string; push_supported: boolean }> {
    return this.request("/api/v1/health");
  }

  async search(tmdbId: number, refresh = false): Promise<SearchResponse> {
    return this.request<SearchResponse>("/api/v1/search", {
      method: "POST",
      body: JSON.stringify({ tmdb_id: tmdbId, refresh }),
    });
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
