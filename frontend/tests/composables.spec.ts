import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../src/api";
import { useAuth } from "../src/composables/useAuth";
import { useCapabilities } from "../src/composables/useCapabilities";
import { useConnectivity } from "../src/composables/useConnectivity";
import { useFavoritesHistory } from "../src/composables/useFavoritesHistory";

// jsdom/Node 实验性 localStorage 在此环境不可用，注入最小内存实现。
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: (key: string): string | null => store[key] ?? null,
    setItem: (key: string, value: string): void => { store[key] = String(value); },
    removeItem: (key: string): void => { delete store[key]; },
    clear: (): void => { store = {}; },
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, configurable: true });

describe("useAuth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("checkSession 成功时 authenticated=true", async () => {
    vi.spyOn(ApiClient.prototype, "me").mockResolvedValue({ authenticated: true, via_bearer: false, csrf_token: "t" } as never);
    const auth = useAuth(new ApiClient(), vi.fn());
    expect(auth.authenticated.value).toBe(false);
    await expect(auth.checkSession()).resolves.toBe(true);
    expect(auth.authenticated.value).toBe(true);
  });

  it("checkSession 失败时 authenticated=false", async () => {
    vi.spyOn(ApiClient.prototype, "me").mockRejectedValue(new ApiError("未登录", 401, "unauthorized"));
    const auth = useAuth(new ApiClient(), vi.fn());
    await expect(auth.checkSession()).resolves.toBe(false);
    expect(auth.authenticated.value).toBe(false);
  });

  it("login 成功后触发 onAuthenticated 并清空密码", async () => {
    vi.spyOn(ApiClient.prototype, "login").mockResolvedValue(undefined as never);
    const onAuth = vi.fn().mockResolvedValue(undefined);
    const auth = useAuth(new ApiClient(), onAuth);
    auth.username.value = "admin";
    auth.password.value = "secret";
    await auth.login();
    expect(auth.authenticated.value).toBe(true);
    expect(auth.password.value).toBe("");
    expect(onAuth).toHaveBeenCalledTimes(1);
  });

  it("login 凭据错误时设置错误文案且不触发 onAuthenticated", async () => {
    vi.spyOn(ApiClient.prototype, "login").mockRejectedValue(new ApiError("账号或密码不正确", 401, "invalid_credentials"));
    const onAuth = vi.fn();
    const auth = useAuth(new ApiClient(), onAuth);
    auth.username.value = "admin";
    auth.password.value = "wrong";
    await auth.login();
    expect(auth.authenticated.value).toBe(false);
    expect(auth.error.value).toBe("账号或密码不正确");
    expect(onAuth).not.toHaveBeenCalled();
  });

  it("登录期间并发 login 调用被忽略", async () => {
    let resolveLogin: () => void = () => {};
    vi.spyOn(ApiClient.prototype, "login").mockImplementation(() => new Promise<void>((resolve) => { resolveLogin = resolve; }));
    const auth = useAuth(new ApiClient(), vi.fn());
    const first = auth.login();
    await auth.login(); // 第二次直接 return
    resolveLogin();
    await first;
    expect(auth.loggingIn.value).toBe(false);
  });
});

describe("useConnectivity", () => {
  it("初始状态是布尔在线值", () => {
    const c = useConnectivity();
    expect(typeof c.isOnline.value).toBe("boolean");
    expect(c.offlineDataAt.value).toBeNull();
  });

  it("recordOfflineData 记录缓存时间戳", () => {
    const c = useConnectivity();
    c.recordOfflineData(new CustomEvent("watch-assistant:offline-data", { detail: { cachedAt: "2026-08-12T00:00:00Z" } }));
    expect(c.offlineDataAt.value).toBe("2026-08-12T00:00:00Z");
  });
});

describe("useFavoritesHistory", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  const movie = (id: number): never => ({ tmdb_id: id, media_type: "movie", title: `片${id}` }) as never;

  it("toggleFavorite 添加并去重", () => {
    const fh = useFavoritesHistory();
    fh.toggleFavorite(movie(1));
    fh.toggleFavorite(movie(2));
    expect(fh.favorites.value.map((m) => m.tmdb_id)).toEqual([2, 1]);
    fh.toggleFavorite(movie(1));
    expect(fh.favorites.value.map((m) => m.tmdb_id)).toEqual([2]);
  });

  it("收藏持久化到 localStorage", () => {
    const fh = useFavoritesHistory();
    fh.toggleFavorite(movie(7));
    expect(localStorage.getItem("watch-assistant:favorites")).toContain('"tmdb_id":7');
  });

  it("recordHistory 置顶去重并限制 50 条", () => {
    const fh = useFavoritesHistory();
    for (let id = 1; id <= 60; id += 1) fh.recordHistory(movie(id));
    expect(fh.history.value.length).toBe(50);
    expect(fh.history.value[0].tmdb_id).toBe(60);
  });
});

describe("useCapabilities", () => {
  it("applyHealth 填充能力开关", () => {
    const caps = useCapabilities();
    expect(caps.organizationPlanEnabled.value).toBe(false);
    caps.applyHealth({
      status: "ok",
      push_supported: true,
      inspection_supported: true,
      inspection_auto_start_enabled: true,
      organization_plan_enabled: true,
      organization_execution_enabled: false,
      organization_execution_supported: false,
      strm_capabilities: { full: true, incremental: false, cleanup: false, playback: false, playback_contract_verified: false },
    } as never);
    expect(caps.pushCapabilities.value.magnet).toBe(true);
    expect(caps.inspectionSupported.value).toBe(true);
    expect(caps.organizationPlanEnabled.value).toBe(true);
    expect(caps.strmFullCapability.value.enabled).toBe(true);
    expect(caps.strmIncrementalCapability.value.enabled).toBe(false);
  });
});
