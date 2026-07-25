<script setup lang="ts">
import { Clock3, Film, Flame, Heart, Home, LoaderCircle, LogIn, PanelRight, Search, Settings, Tv, X } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "./api";
import TaskDrawer from "./components/TaskDrawer.vue";
import {
  extractBrowseView,
  extractMediaRoute,
  clampCatalogPage,
  navigateToCatalog,
  navigateToMedia,
  navigateToView,
  parseCatalogRoute,
  type CatalogRoute,
  type BrowseView,
} from "./router";
import { mediaKey, mediaTypeOf } from "./media";
import { canPushResource, NO_PUSH_CAPABILITIES, resolvePushCapabilities, submitPushResource, type PushCapabilities } from "./push";
import { finalizeInspectionResources, inspectionProgress as getInspectionProgress, inspectionResultEnded, inspectionState as getInspectionBatchState, mergeInspectionResult, nextInspectionResourceIds, pollInspectionBatch } from "./inspection";
import type { HomeCatalogResponse, MovieMetadata, ResourceSummary, SearchResponse, TaskResponse } from "./types";
import CollectionView from "./views/CollectionView.vue";
import HomeView from "./views/HomeView.vue";
import LibraryView from "./views/LibraryView.vue";
import MovieView from "./views/MovieView.vue";
import SearchView from "./views/SearchView.vue";
import SettingsView from "./views/SettingsView.vue";

const FAVORITES_KEY = "watch-assistant:favorites";
const HISTORY_KEY = "watch-assistant:history";
const api = new ApiClient();
const password = ref("");
const query = ref("");
const searchInput = ref("");
const authenticated = ref(false);
const loading = ref(false);
const catalogLoading = ref(false);
const error = ref("");
const result = ref<SearchResponse | null>(null);
const homeCatalog = ref<HomeCatalogResponse | null>(null);
const catalogMovies = ref<MovieMetadata[]>([]);
const catalogHeading = ref("");
const activeView = ref<BrowseView>("home");
const previousView = ref<BrowseView>("home");
const genreId = ref<number | undefined>();
const year = ref<number | undefined>();
const sort = ref<"popular" | "rating" | "release">("popular");
const currentPage = ref(1);
const totalPages = ref(1);
const totalResults = ref(0);
const favorites = ref<MovieMetadata[]>(readStoredMovies(FAVORITES_KEY));
const history = ref<MovieMetadata[]>(readStoredMovies(HISTORY_KEY));
const tasks = ref<TaskResponse[]>([]);
const pushingId = ref<string | null>(null);
const drawerOpen = ref(false);
const pushCapabilities = ref<PushCapabilities>({ ...NO_PUSH_CAPABILITIES });
const inspectionSupported = ref(false);
const selectedSeason = ref<number | null>(null);
const detailMediaType = ref<"movie" | "tv">("movie");
const inspectionState = ref<"idle" | "running" | "completed" | "partial" | "failed" | "timeout">("idle");
const inspectionCompleted = ref(0);
const inspectionTotal = ref(0);
const inspectionFailed = ref(0);
const inspectionError = ref<string | null>(null);
const inspectionProcessedIds = ref<Set<string>>(new Set());
const inspectionInFlightIds = ref<Set<string>>(new Set());
const inspectionRetryIds = ref<Set<string>>(new Set());
const inspectionAutoRequestId = ref<number | null>(null);
let searchRequestId = 0;
let inspectionRunId = 0;
let pollTimer: number | undefined;
let catalogRequestId = 0;

interface CatalogCacheEntry {
  movies: MovieMetadata[];
  page: number;
  totalPages: number;
  totalResults: number;
}

const catalogCache = new Map<string, CatalogCacheEntry>();
const committedCatalogRoute = ref<CatalogRoute | null>(null);
let pendingCatalogRoute: CatalogRoute | null = null;
let catalogReturnRoute: CatalogRoute | null = null;
let catalogReturnScrollY: number | null = null;

const favoriteIds = computed(() => new Set(favorites.value.map(mediaKey)));
const detailFavorite = computed(() => result.value ? favoriteIds.value.has(mediaKey(result.value.movie)) : false);
const hasActiveTasks = computed(() => tasks.value.some((task) => task.state === "queued" || task.state === "submitting"));

function catalogCacheKey(route: CatalogRoute): string {
  return [route.view, route.query.trim(), route.genreId ?? "", route.year ?? "", route.sort, route.page].join("|");
}

function normalizeCatalogRoute(route: CatalogRoute): CatalogRoute {
  const genreId = Number.isInteger(route.genreId) && route.genreId >= 1 ? route.genreId : undefined;
  const year = Number.isInteger(route.year) && route.year >= 1900 && route.year <= 2100 ? route.year : undefined;
  return {
    view: route.view,
    query: route.query.trim(),
    page: clampCatalogPage(route.page),
    sort: route.sort,
    ...(genreId !== undefined ? { genreId } : {}),
    ...(year !== undefined ? { year } : {}),
  };
}

function sameCatalogRoute(left: CatalogRoute, right: CatalogRoute): boolean {
  return left.view === right.view
    && left.query === right.query
    && left.page === right.page
    && left.genreId === right.genreId
    && left.year === right.year
    && left.sort === right.sort;
}

function readCatalogCache(route: CatalogRoute): CatalogCacheEntry | undefined {
  const key = catalogCacheKey(route);
  const entry = catalogCache.get(key);
  if (!entry) return undefined;
  catalogCache.delete(key);
  catalogCache.set(key, entry);
  return entry;
}

function writeCatalogCache(route: CatalogRoute, entry: CatalogCacheEntry) {
  const key = catalogCacheKey(route);
  catalogCache.delete(key);
  catalogCache.set(key, entry);
  while (catalogCache.size > 20) catalogCache.delete(catalogCache.keys().next().value as string);
}

function applyCatalogPreview(route: CatalogRoute) {
  activeView.value = route.view;
  query.value = route.query;
  searchInput.value = route.query;
  genreId.value = route.genreId;
  year.value = route.year;
  sort.value = route.sort;
  catalogHeading.value = route.view === "search" ? `“${route.query}”的搜索结果` : route.view === "popular" ? "本周热门" : "";
}

function applyCatalogData(route: CatalogRoute, entry: CatalogCacheEntry) {
  applyCatalogPreview(route);
  catalogMovies.value = entry.movies;
  currentPage.value = entry.page;
  totalPages.value = entry.totalPages;
  totalResults.value = entry.totalResults;
}

function catalogScroll(restoreY?: number) {
  void nextTick(() => {
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    if (restoreY !== undefined) {
      window.scrollTo({ top: restoreY, behavior });
      return;
    }
    document.getElementById("catalog-title")?.scrollIntoView({ behavior, block: "start" });
  });
}

function commitCatalogRoute(route: CatalogRoute, historyMode: "push" | "replace" | "none", scrollY = 0) {
  committedCatalogRoute.value = { ...route };
  if (historyMode === "push") navigateToCatalog(route, false, { catalogScrollY: scrollY });
  else if (historyMode === "replace") navigateToCatalog(route, true, { catalogScrollY: scrollY });
  else navigateToCatalog(route, true, { ...window.history.state, catalogScrollY: window.history.state?.catalogScrollY ?? scrollY });
}

async function requestCatalog(route: CatalogRoute, historyMode: "push" | "replace" | "none", restoreY?: number) {
  const safeRoute = normalizeCatalogRoute(route);
  if (historyMode === "push" && committedCatalogRoute.value && sameCatalogRoute(safeRoute, committedCatalogRoute.value)) return;
  const requestId = ++catalogRequestId;
  pendingCatalogRoute = safeRoute;
  const isCurrent = () => requestId === catalogRequestId && pendingCatalogRoute === safeRoute;
  catalogLoading.value = true;
  error.value = "";

  const cached = readCatalogCache(safeRoute);
  if (cached) {
    if (!isCurrent()) return;
    const committedRoute = { ...safeRoute, page: cached.page };
    applyCatalogData(committedRoute, cached);
    pendingCatalogRoute = null;
    catalogLoading.value = false;
    commitCatalogRoute(committedRoute, historyMode, restoreY ?? 0);
    catalogScroll(restoreY);
    return;
  }

  try {
    const response = safeRoute.view === "search"
      ? await api.searchMedia(safeRoute.query, safeRoute.page)
      : safeRoute.view === "popular"
        ? await api.popularMovies(safeRoute.page)
        : await api.discoverMedia(safeRoute.view === "tv" ? "tv" : "movie", {
            genreId: safeRoute.genreId,
            year: safeRoute.year,
            sort: safeRoute.sort,
            page: safeRoute.page,
          });
    if (!isCurrent()) return;
    const entry: CatalogCacheEntry = {
      movies: response.results,
      page: clampCatalogPage(Math.trunc(response.page)),
      totalPages: clampCatalogPage(Math.trunc(response.total_pages)),
      totalResults: response.total_results,
    };
    writeCatalogCache(safeRoute, entry);
    const committedRoute = { ...safeRoute, page: entry.page };
    if (!sameCatalogRoute(safeRoute, committedRoute)) writeCatalogCache(committedRoute, entry);
    applyCatalogData(committedRoute, entry);
    pendingCatalogRoute = null;
    catalogLoading.value = false;
    commitCatalogRoute(committedRoute, historyMode, restoreY ?? 0);
    catalogScroll(restoreY);
  } catch (exception) {
    if (!isCurrent()) return;
    pendingCatalogRoute = null;
    catalogLoading.value = false;
    error.value = exception instanceof ApiError ? exception.message : "目录加载失败，请稍后重试";
  }
}
function inspectionBatchIds(limit = 8, retriesOnly = false): string[] {
  if (!result.value) return [];
  const unavailableIds = new Set([...inspectionProcessedIds.value, ...inspectionInFlightIds.value]);
  const candidates = nextInspectionResourceIds(
    result.value.results,
    unavailableIds,
    30,
  );
  const eligible = retriesOnly
    ? candidates.filter((resourceId) => inspectionRetryIds.value.has(resourceId))
    : candidates.filter((resourceId) => !inspectionRetryIds.value.has(resourceId));
  return eligible.slice(0, limit);
}

const inspectionRetryAvailable = computed(() => inspectionBatchIds(8, true).length > 0);
const inspectionMoreAvailable = computed(() => {
  if (!inspectionSupported.value || !result.value || inspectionState.value === "running") return false;
  if (inspectionAutoRequestId.value !== searchRequestId) return false;
  return inspectionBatchIds(1).length > 0;
});

const navItems = [
  { view: "home" as const, label: "首页", icon: Home },
  { view: "movies" as const, label: "电影", icon: Film },
  { view: "tv" as const, label: "剧集", icon: Tv },
  { view: "popular" as const, label: "热门", icon: Flame },
];

function readStoredMovies(key: string): MovieMetadata[] {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "[]");
    return Array.isArray(value)
      ? value.filter((movie) => Number.isInteger(movie?.tmdb_id) && typeof movie?.title === "string")
      : [];
  } catch {
    return [];
  }
}

function storeMovies(key: string, movies: MovieMetadata[]) {
  localStorage.setItem(key, JSON.stringify(movies));
}

function toggleFavorite(movie: MovieMetadata) {
  const key = mediaKey(movie);
  favorites.value = favoriteIds.value.has(key)
    ? favorites.value.filter((item) => mediaKey(item) !== key)
    : [movie, ...favorites.value].slice(0, 100);
  storeMovies(FAVORITES_KEY, favorites.value);
}

function recordHistory(movie: MovieMetadata) {
  history.value = [movie, ...history.value.filter((item) => mediaKey(item) !== mediaKey(movie))].slice(0, 50);
  storeMovies(HISTORY_KEY, history.value);
}

async function loadHome() {
  if (homeCatalog.value) return;
  catalogLoading.value = true;
  error.value = "";
  try {
    homeCatalog.value = await api.homeCatalog();
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "首页内容加载失败，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function loadDiscover(
  filters = { genreId: genreId.value, year: year.value, sort: sort.value },
  page = 1,
  historyMode: "push" | "none" = "push",
  targetView?: "movies" | "tv",
) {
  const view = targetView ?? (activeView.value === "tv" ? "tv" : "movies");
  await requestCatalog({ view, query: "", page, genreId: filters.genreId, year: filters.year, sort: filters.sort }, historyMode);
}

async function loadPopular(page = 1, historyMode: "push" | "none" = "push") {
  await requestCatalog({ view: "popular", query: "", page, sort: "popular" }, historyMode);
}

async function performSearch(updateUrl: boolean, page = 1) {
  const searchQuery = searchInput.value.trim();
  if (!searchQuery) return;
  invalidateDetailRequest();
  previousView.value = "search";
  result.value = null;
  await requestCatalog({ view: "search", query: searchQuery, page, sort: "popular" }, updateUrl ? "push" : "none");
}

async function searchMovies() {
  await performSearch(true);
}

async function loadPage(page: number) {
  if (catalogLoading.value) return;
  const current = committedCatalogRoute.value;
  if (!current) return;
  await requestCatalog({ ...current, page }, "push");
}

async function loadView(view: BrowseView, historyMode: "push" | "none" = "none") {
  if (view === "home") await loadHome();
  if (view === "movies") await loadDiscover({ genreId: undefined, year: undefined, sort: "popular" }, 1, historyMode, "movies");
  if (view === "tv") await loadDiscover({ genreId: undefined, year: undefined, sort: "popular" }, 1, historyMode, "tv");
  if (view === "popular") await loadPopular(1, historyMode);
  if (view === "search") await performSearch(false);
}

async function selectView(view: Exclude<BrowseView, "search">) {
  invalidateDetailRequest();
  result.value = null;
  error.value = "";
  previousView.value = view;
  if (view === "movies" || view === "tv") {
    await loadDiscover({ genreId: undefined, year: undefined, sort: "popular" }, 1, "push", view);
  } else if (view === "popular") {
    await loadPopular(1, "push");
  } else {
    committedCatalogRoute.value = null;
    catalogReturnRoute = null;
    catalogReturnScrollY = null;
    activeView.value = view;
    navigateToView(view);
    await loadView(view);
  }
}

function resetInspection() {
  inspectionRunId += 1;
  inspectionState.value = "idle";
  inspectionCompleted.value = 0;
  inspectionTotal.value = 0;
  inspectionFailed.value = 0;
  inspectionError.value = null;
  inspectionProcessedIds.value = new Set();
  inspectionInFlightIds.value = new Set();
  inspectionRetryIds.value = new Set();
  inspectionAutoRequestId.value = null;
}

function invalidateDetailRequest() {
  searchRequestId += 1;
  catalogRequestId += 1;
  pendingCatalogRoute = null;
  catalogLoading.value = false;
  loading.value = false;
  resetInspection();
}

async function loadResources(
  id: number,
  mediaType: "movie" | "tv",
  refresh = false,
  seasonNumber: number | null = selectedSeason.value,
  clearResult = false,
) {
  const requestId = ++searchRequestId;
  resetInspection();
  detailMediaType.value = mediaType;
  if (clearResult) result.value = null;
  loading.value = true;
  error.value = "";
  try {
    const response = await api.search(id, mediaType, refresh, mediaType === "tv" ? seasonNumber ?? undefined : undefined);
    if (requestId !== searchRequestId) return;
    result.value = response;
    selectedSeason.value = mediaType === "tv"
      ? Object.prototype.hasOwnProperty.call(response, "selected_season")
        ? response.selected_season ?? null
        : seasonNumber
      : null;
    recordHistory(response.movie);
    startAutomaticInspection(requestId);
  } catch (exception) {
    if (requestId === searchRequestId) {
      error.value = exception instanceof ApiError ? exception.message : "资源搜索失败，请稍后重试";
    }
  } finally {
    if (requestId === searchRequestId) loading.value = false;
  }
}

async function openMovie(movie: MovieMetadata) {
  previousView.value = activeView.value;
  if (committedCatalogRoute.value) {
    catalogReturnRoute = { ...committedCatalogRoute.value };
    catalogReturnScrollY = window.scrollY;
    window.history.replaceState({ ...window.history.state, catalog: catalogReturnRoute, catalogScrollY: catalogReturnScrollY }, "", window.location.href);
  } else {
    catalogReturnRoute = null;
    catalogReturnScrollY = null;
  }
  recordHistory(movie);
  result.value = null;
  selectedSeason.value = null;
  const mediaType = mediaTypeOf(movie);
  navigateToMedia(mediaType, movie.tmdb_id);
  if (catalogReturnRoute) {
    window.history.replaceState({
      ...window.history.state,
      catalog: catalogReturnRoute,
      catalogScrollY: catalogReturnScrollY,
      catalogDetailEntry: true,
    }, "", window.location.href);
  }
  await loadResources(movie.tmdb_id, mediaType, false, null, true);
}

async function selectSeason(seasonNumber: number | null) {
  if (!result.value || detailMediaType.value !== "tv") return;
  const movie = result.value.movie;
  selectedSeason.value = seasonNumber;
  navigateToMedia("tv", movie.tmdb_id, seasonNumber ?? undefined);
  await loadResources(movie.tmdb_id, "tv", false, seasonNumber, true);
}

async function returnToBrowse() {
  invalidateDetailRequest();
  result.value = null;
  if (catalogReturnRoute && window.history.state?.catalogDetailEntry === true) {
    catalogReturnRoute = null;
    catalogReturnScrollY = null;
    window.history.back();
    return;
  }
  const view = previousView.value === "search" ? "search" : previousView.value;
  activeView.value = view;
  navigateToView(view);
  await loadView(view);
}

async function initializeWorkspace() {
  const mediaRoute = extractMediaRoute(window.location.pathname + window.location.search);
  if (mediaRoute !== null) {
    selectedSeason.value = mediaRoute.mediaType === "tv" ? mediaRoute.seasonNumber ?? null : null;
    await loadResources(mediaRoute.tmdbId, mediaRoute.mediaType, false, selectedSeason.value, true);
    return;
  }
  const catalogRoute = parseCatalogRoute(window.location.pathname + window.location.search);
  if (catalogRoute) {
    if (catalogRoute.view === "search" && !catalogRoute.query.trim()) {
      activeView.value = "home";
      previousView.value = "home";
      navigateToView("home");
      await loadHome();
      return;
    }
    await requestCatalog(catalogRoute, "none", typeof window.history.state?.catalogScrollY === "number" ? window.history.state.catalogScrollY : undefined);
    return;
  }
  activeView.value = extractBrowseView(window.location.pathname);
  previousView.value = activeView.value;
  await loadView(activeView.value);
}

async function login() {
  error.value = "";
  try {
    await api.login(password.value);
    authenticated.value = true;
    password.value = "";
    await initializeWorkspace();
  } catch (exception) {
    error.value = exception instanceof ApiError ? "密码不正确" : "登录失败";
  }
}

async function push(resource: ResourceSummary) {
  if (!canPushResource(resource, pushCapabilities.value)) {
    error.value = resource.kind === "115_share" ? "115 分享转存尚未验证" : "磁力云下载不可用";
    return;
  }
  pushingId.value = resource.resource_id;
  error.value = "";
  try {
    const task = await submitPushResource(resource, pushCapabilities.value, (resourceId) => api.createTask(resourceId));
    if (!task) {
      error.value = resource.kind === "115_share" ? "115 分享转存尚未验证" : "磁力云下载不可用";
      return;
    }
    tasks.value = [task, ...tasks.value.filter((item) => item.id !== task.id)].slice(0, 50);
    drawerOpen.value = true;
    ensurePolling();
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "推送失败";
  } finally {
    pushingId.value = null;
  }
}

function applyInspectionResponse(response: Parameters<typeof getInspectionProgress>[0], resourceIds: string[]) {
  if (!result.value) return;
  const progress = getInspectionProgress(response);
  inspectionCompleted.value = progress.completed;
  inspectionTotal.value = progress.submitted;
  inspectionFailed.value = progress.failed;
  const byId = new Map(response.results.map((item) => [item.resource_id, item]));
  result.value = {
    ...result.value,
    results: result.value.results.map((resource) => {
      const item = byId.get(resource.resource_id);
      if (!item) return resource;
      return mergeInspectionResult(resource, item);
    }),
  };

  const targetIds = new Set(resourceIds);
  const processedIds = new Set(inspectionProcessedIds.value);
  const inFlightIds = new Set(inspectionInFlightIds.value);
  const retryIds = new Set(inspectionRetryIds.value);
  response.results.forEach((item) => {
    if (!targetIds.has(item.resource_id) || !inspectionResultEnded(item)) return;
    inFlightIds.delete(item.resource_id);
    if (item.status === "verified" || item.status === "unsupported") {
      processedIds.add(item.resource_id);
      retryIds.delete(item.resource_id);
    } else {
      processedIds.delete(item.resource_id);
      retryIds.add(item.resource_id);
    }
  });
  inspectionProcessedIds.value = processedIds;
  inspectionInFlightIds.value = inFlightIds;
  inspectionRetryIds.value = retryIds;
}

function setInspectionStatus(resourceIds: string[], status: string) {
  if (!result.value) return;
  const ids = new Set(resourceIds);
  result.value = {
    ...result.value,
    results: result.value.results.map((resource) => ids.has(resource.resource_id)
      ? { ...resource, inspection_status: status as "running" }
      : resource),
  };
}

function finalizeInspection(resourceIds: string[], status: "failed" | "timeout") {
  if (!result.value) return;
  result.value = {
    ...result.value,
    results: finalizeInspectionResources(result.value.results, resourceIds, status),
  };
}

function inspectionErrorFor(state: "partial" | "failed", failed: number): string {
  if (state === "failed") return "检测失败";
  return failed > 0 ? `部分失败：${failed} 条磁力检测失败` : "部分失败";
}

function unresolvedInspectionIds(resourceIds: string[]): string[] {
  if (!result.value) return [];
  const byId = new Map(result.value.results.map((resource) => [resource.resource_id, resource]));
  return resourceIds.filter((resourceId) => {
    const status = byId.get(resourceId)?.inspection_status;
    return inspectionInFlightIds.value.has(resourceId) || status === "running" || status === "queued";
  });
}

function releaseInspectionIds(resourceIds: string[], status: "failed" | "timeout") {
  const unresolvedIds = unresolvedInspectionIds(resourceIds);
  if (!unresolvedIds.length) return;
  inspectionInFlightIds.value = new Set(
    [...inspectionInFlightIds.value].filter((resourceId) => !unresolvedIds.includes(resourceId)),
  );
  inspectionRetryIds.value = new Set([...inspectionRetryIds.value, ...unresolvedIds]);
  finalizeInspection(unresolvedIds, status);
}

function applyTerminalInspectionState(
  resourceIds: string[],
  state: "completed" | "partial" | "failed",
) {
  releaseInspectionIds(resourceIds, "failed");
  if (state !== "completed") {
    inspectionError.value = inspectionErrorFor(state, inspectionFailed.value);
  }
  inspectionState.value = state;
}

async function inspectBatch(resourceIds: string[], requestId: number) {
  if (!result.value || !inspectionSupported.value || requestId !== searchRequestId || inspectionState.value === "running") return;

  const runId = ++inspectionRunId;
  inspectionInFlightIds.value = new Set([...inspectionInFlightIds.value, ...resourceIds]);
  inspectionState.value = "running";
  inspectionCompleted.value = 0;
  inspectionTotal.value = 0;
  inspectionFailed.value = 0;
  inspectionError.value = null;
  setInspectionStatus(resourceIds, "running");
  const isCurrent = () => requestId === searchRequestId && runId === inspectionRunId;

  try {
    const started = await api.inspectResources(resourceIds);
    if (!isCurrent()) return;
    applyInspectionResponse(started, resourceIds);
    const startedState = getInspectionBatchState(started);
    if (startedState === "completed" || startedState === "partial" || startedState === "failed") {
      applyTerminalInspectionState(resourceIds, startedState);
      return;
    }
    const pollState = await pollInspectionBatch(
      (batchId, signal) => api.getInspection(batchId, signal),
      started.batch_id,
      {
        isCurrent,
        onResponse: (response) => applyInspectionResponse(response, resourceIds),
      },
    );
    if (!isCurrent()) return;
    if (pollState === "stale") return;
    if (pollState === "timeout") {
      releaseInspectionIds(resourceIds, "timeout");
      inspectionState.value = "timeout";
      inspectionError.value = "检测超时，可重试";
      return;
    }
    if (pollState === "completed" || pollState === "partial" || pollState === "failed") applyTerminalInspectionState(resourceIds, pollState);
  } catch (exception) {
    if (!isCurrent()) return;
    releaseInspectionIds(resourceIds, "failed");
    inspectionState.value = "failed";
    inspectionError.value = exception instanceof ApiError ? exception.message : "检测失败，可重试";
  }
}

function startAutomaticInspection(requestId: number) {
  if (inspectionAutoRequestId.value === requestId) return;
  inspectionAutoRequestId.value = requestId;
  if (!inspectionSupported.value || !result.value) return;
  const resourceIds = inspectionBatchIds();
  if (resourceIds.length) void inspectBatch(resourceIds, requestId);
}

function inspectMore() {
  if (!result.value || !inspectionMoreAvailable.value) return;
  const resourceIds = inspectionBatchIds(8);
  if (resourceIds.length) void inspectBatch(resourceIds, searchRequestId);
}

function retryFailed() {
  if (!result.value || !inspectionRetryAvailable.value) return;
  const resourceIds = inspectionBatchIds(8, true);
  if (resourceIds.length) void inspectBatch(resourceIds, searchRequestId);
}

function ensurePolling() {
  if (pollTimer !== undefined) return;
  pollTimer = window.setInterval(async () => {
    if (!hasActiveTasks.value) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
      return;
    }
    const active = tasks.value.filter((task) => task.state === "queued" || task.state === "submitting");
    const updates = await Promise.all(active.map((task) => api.getTask(task.id).catch(() => task)));
    const byId = new Map(updates.map((task) => [task.id, task]));
    tasks.value = tasks.value.map((task) => byId.get(task.id) ?? task);
  }, 2000);
}

async function syncRoute() {
  invalidateDetailRequest();
  result.value = null;
  await initializeWorkspace();
}

onMounted(async () => {
  try {
    const health = await api.health();
    pushCapabilities.value = resolvePushCapabilities(health);
    inspectionSupported.value = health.inspection_supported === true;
    await api.me();
    authenticated.value = true;
  } catch {
    authenticated.value = false;
    window.addEventListener("popstate", syncRoute);
    return;
  }
  await initializeWorkspace();
  window.addEventListener("popstate", syncRoute);
});

onBeforeUnmount(() => {
  invalidateDetailRequest();
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
  window.removeEventListener("popstate", syncRoute);
});
</script>

<template>
  <main class="app-shell">
    <header class="topbar" :class="{ authenticated }">
      <a class="brand" href="/">WATCH<span>/</span>ASSISTANT</a>
      <template v-if="authenticated">
        <nav class="primary-nav" aria-label="主导航">
          <button v-for="item in navItems" :key="item.view" type="button" :class="{ active: activeView === item.view && !result }" @click="selectView(item.view)"><component :is="item.icon" :size="16" />{{ item.label }}</button>
        </nav>
        <form class="top-search" role="search" @submit.prevent="searchMovies"><Search :size="17" /><input v-model="searchInput" type="search" aria-label="搜索电影或电视剧" placeholder="搜索电影或电视剧" /><button type="submit" aria-label="提交搜索" title="搜索"><Search :size="17" /></button></form>
        <div class="topbar-actions">
          <button type="button" :class="{ active: activeView === 'favorites' && !result }" @click="selectView('favorites')"><Heart :size="17" />收藏</button>
          <button type="button" :class="{ active: activeView === 'history' && !result }" @click="selectView('history')"><Clock3 :size="17" />记录</button>
          <button class="icon-button" type="button" title="推送记录" aria-label="推送记录" @click="drawerOpen = true"><PanelRight :size="18" /></button>
          <button class="icon-button" type="button" :class="{ active: activeView === 'settings' && !result }" title="设置" aria-label="设置" @click="selectView('settings')"><Settings :size="18" /></button>
        </div>
      </template>
      <div v-else class="topbar-meta"><span class="status-dot" />LAN workspace</div>
    </header>

    <section v-if="!authenticated" class="auth-gate"><div class="auth-mark"><LogIn :size="20" /></div><p class="eyebrow">PRIVATE WORKSPACE</p><h1>进入观影工作台</h1><p>你的 PanSou 聚合和 115 推送只在本地网络可见。</p><form @submit.prevent="login"><label for="password">Web 密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入访问密码" /><button class="primary-button" type="submit"><LogIn :size="17" />登录</button></form><p v-if="error" class="error-text">{{ error }}</p></section>

    <template v-else>
      <p v-if="error" class="error-strip"><X :size="16" />{{ error }}</p>
      <template v-if="!result && !loading">
        <HomeView v-if="activeView === 'home'" :catalog="homeCatalog" :loading="catalogLoading" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" @navigate="selectView" />
        <LibraryView v-else-if="activeView === 'movies' || activeView === 'tv'" :movies="catalogMovies" :loading="catalogLoading" :favorite-ids="favoriteIds" :genre-id="genreId" :year="year" :sort="sort" :media-type="activeView" :page="currentPage" :total-pages="totalPages" :total-results="totalResults" @open="openMovie" @favorite="toggleFavorite" @filters="loadDiscover" @page="loadPage" />
        <CollectionView v-else-if="activeView === 'favorites' || activeView === 'history'" :mode="activeView" :movies="activeView === 'favorites' ? favorites : history" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" />
        <SettingsView v-else-if="activeView === 'settings'" :api="api" />
        <SearchView v-else v-model="searchInput" :loading="catalogLoading" :movies="catalogMovies" :heading="catalogHeading" :favorite-ids="favoriteIds" :page="currentPage" :total-pages="totalPages" :total-results="totalResults" @search="searchMovies" @reset="selectView('home')" @open="openMovie" @favorite="toggleFavorite" @page="loadPage" />
      </template>
      <section v-else-if="loading && !result" class="detail-loading"><LoaderCircle class="spin" :size="24" /><strong>正在聚合资源</strong><span>正在查询 PanSou 的磁力与 115 分享结果</span></section>
       <p v-if="result && !pushCapabilities.magnet && !pushCapabilities.share" class="warning-strip">115 推送当前不可用，推送按钮已禁用。</p>
       <p v-else-if="result && pushCapabilities.magnet && !pushCapabilities.share" class="warning-strip">磁力云下载可用，115 分享转存尚未验证</p>
       <section v-if="result" class="detail-workspace"><MovieView :result="result" :media-type="detailMediaType" :season-number="selectedSeason" :pushing-id="pushingId" :push-capabilities="pushCapabilities" :favorite="detailFavorite" :inspection-supported="inspectionSupported" :inspection-state="inspectionState" :inspection-completed="inspectionCompleted" :inspection-total="inspectionTotal" :inspection-failed="inspectionFailed" :inspection-error="inspectionError" :inspection-more-available="inspectionMoreAvailable" :inspection-retry-available="inspectionRetryAvailable" @push="push" @favorite="toggleFavorite(result.movie)" @refresh="loadResources(result.movie.tmdb_id, detailMediaType, true)" @season="selectSeason" @inspect-more="inspectMore" @retry-failed="retryFailed" @back="returnToBrowse" /></section>
    </template>
    <TaskDrawer :tasks="tasks" :open="drawerOpen" @close="drawerOpen = false" />
  </main>
</template>
