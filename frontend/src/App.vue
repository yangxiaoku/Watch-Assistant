<script setup lang="ts">
import { Bell, ClipboardCheck, Clock3, Database, Film, Flame, Heart, Home, LayoutDashboard, ListTodo, LoaderCircle, LogIn, PanelRight, Search, Settings, Tv, X } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError, browserIsOnline, focusFirstFieldError } from "./api";
import TaskDrawer from "./components/TaskDrawer.vue";
import {
  extractBrowseView,
  extractMediaRoute,
  clampCatalogPage,
  navigateToCatalog,
  navigateToMedia,
  mediaRoutePath,
  navigateToView,
  parseCatalogRoute,
  type CatalogRoute,
  type BrowseView,
  type MediaResourceRouteState,
} from "./router";
import { mediaKey, mediaTypeOf } from "./media";
import { canPushResource, NO_PUSH_CAPABILITIES, resolvePushCapabilities, submitPushResource, type PushCapabilities } from "./push";
import { finalizeInspectionResources, inspectionProgress as getInspectionProgress, inspectionResultEnded, inspectionState as getInspectionBatchState, MAX_INSPECTABLE_MAGNETS, mergeInspectionResult, nextInspectionResourceIds, pollInspectionBatch } from "./inspection";
import { describeUiError } from "./errorCatalog";
import { waitForResourceSearch as pollResourceSearch } from "./resourceSearchPolling";
import { sourceNameList } from "./resourceSources";
import { createTaskRefreshGuard, isActiveTask } from "./taskPolling";
import type { CapabilityAvailability, HomeCatalogResponse, MovieMetadata, ResourceFacets, ResourcePageResponse, ResourceQuality, ResourceSearchResponse, ResourceSort, ResourceSummary, SearchResponse, SeasonDetailResponse, TaskResponse } from "./types";
import CollectionView from "./views/CollectionView.vue";
import HomeView from "./views/HomeView.vue";
import LibraryView from "./views/LibraryView.vue";
import MovieView from "./views/MovieView.vue";
import SearchView from "./views/SearchView.vue";
import SettingsView from "./views/SettingsView.vue";
import LibraryWorkbenchView from "./views/LibraryWorkbenchView.vue";
import WorkflowCenterView from "./views/WorkflowCenterView.vue";
import NotificationCenterView from "./views/NotificationCenterView.vue";
import OrganizationHistoryView from "./views/OrganizationHistoryView.vue";
import OrganizationWorkbenchView from "./views/OrganizationWorkbenchView.vue";
import WorkbenchView from "./views/WorkbenchView.vue";

const FAVORITES_KEY = "watch-assistant:favorites";
const HISTORY_KEY = "watch-assistant:history";
const api = new ApiClient();
const username = ref("admin");
const password = ref("");
const query = ref("");
const searchInput = ref("");
const authenticated = ref(false);
const isOnline = ref(browserIsOnline());
const offlineDataAt = ref<string | null>(null);
const loading = ref(false);
const catalogLoading = ref(false);
const error = ref("");
const result = ref<SearchResponse | null>(null);
const searchSourceNames = ref<string[]>([]);
const metadataLoading = ref(false);
const metadataError = ref("");
const metadataStale = ref(false);
const homeCatalog = ref<HomeCatalogResponse | null>(null);
const homeError = ref("");
const catalogMovies = ref<MovieMetadata[]>([]);
const catalogHeading = ref("");
const catalogError = ref("");
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
const activeWorkflowId = ref<string | null>(null);
const pushingId = ref<string | null>(null);
const drawerOpen = ref(false);
const pushCapabilities = ref<PushCapabilities>({ ...NO_PUSH_CAPABILITIES });
const inspectionSupported = ref(false);
const inspectionAutoStartEnabled = ref<boolean | "unknown">("unknown");
const organizationPlanEnabled = ref(false);
const organizationExecutionSupported = ref(false);
const settingsInitialSection = ref<"overview" | "organization">("overview");
const unavailableCapability = (settingsSection: CapabilityAvailability["settings_section"] = "overview"): CapabilityAvailability => ({
  enabled: false,
  reason_code: "capability_unknown",
  reason_zh: "当前未读取能力状态，请刷新页面后重试。",
  settings_section: settingsSection,
});
const strmFullCapability = ref<CapabilityAvailability>(unavailableCapability());
const strmIncrementalCapability = ref<CapabilityAvailability>(unavailableCapability());
const strmCleanupCapability = ref<CapabilityAvailability>(unavailableCapability());
const emptyDirectoryCleanupCapability = ref<CapabilityAvailability>(unavailableCapability());
const organizationPlanCapability = ref<CapabilityAvailability>(unavailableCapability());
const selectedSeason = ref<number | null>(null);
const seasonDetail = ref<SeasonDetailResponse | null>(null);
const seasonDetailLoading = ref(false);
const seasonDetailError = ref("");
const detailMediaType = ref<"movie" | "tv">("movie");
const inspectionState = ref<"idle" | "running" | "completed" | "partial" | "failed" | "timeout">("idle");
const inspectionCompleted = ref(0);
const inspectionTotal = ref(0);
const inspectionFailed = ref(0);
const inspectionError = ref<string | null>(null);
const inspectionProcessedIds = ref<Set<string>>(new Set());
const inspectionInFlightIds = ref<Set<string>>(new Set());
const inspectionRetryIds = ref<Set<string>>(new Set());
const inspectionSeenIds = ref<Set<string>>(new Set());
const inspectionResults = ref<Map<string, import("./types").InspectionResult>>(new Map());
const inspectionStatusOverrides = ref<Map<string, ResourceSummary["inspection_status"]>>(new Map());
const inspectionAutoRequestId = ref<number | null>(null);
const resourceResponse = ref<ResourcePageResponse | null>(null);
const resourceItemsFallback = ref<ResourceSummary[]>([]);
const resourceLoading = ref(false);
const resourceSearchLoading = ref(false);
const resourceError = ref("");
const resourcePaginationUnavailable = ref(false);
const resourcePage = ref(1);
const resourcePageSize = ref<25 | 50 | 100>(25);
const resourceTotal = ref(0);
const resourceHiddenTotal = ref(0);
const resourceTotalPages = ref(1);
const resourceFacets = ref<ResourceFacets>({ magnet: 0, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 });
const resourceKind = ref<"all" | "magnet" | "115_share">("all");
const resourceQuality = ref<"all" | ResourceQuality>("all");
const resourceQuery = ref("");
const resourceSort = ref<ResourceSort>("comprehensive");
let resourceSearchCached = false;
let searchRequestId = 0;
let metadataRequestId = 0;
let inspectionRunId = 0;
let pollTimer: number | undefined;
let catalogRequestId = 0;
let failedCatalogRoute: CatalogRoute | null = null;
let resourceRequestId = 0;
let resourceAbortController: AbortController | null = null;
let resourceSearchAbortController: AbortController | null = null;
let metadataAbortController: AbortController | null = null;
let resourceQueryTimer: number | undefined;
let workflowCreationPromise: Promise<string | null> | null = null;
let pendingResourceRoute: ResourceRouteState | null = null;
let seasonDetailRequestId = 0;
let seasonDetailAbortController: AbortController | null = null;
const taskRefreshGuard = createTaskRefreshGuard();

type DetailMetricStage = "detail_framework" | "metadata_summary" | "metadata_complete" | "metadata_failed" | "resource_first_batch" | "resource_complete" | "resource_failed" | "late_response" | "request_cancelled";
type DetailTiming = { startedAt: number; mediaType: "movie" | "tv"; tmdbId: number; seasonNumber: number | null; reported: Set<DetailMetricStage> };
const detailTimings = new Map<number, DetailTiming>();

function detailClock(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function startDetailTiming(requestId: number, mediaType: "movie" | "tv", tmdbId: number): void {
  detailTimings.set(requestId, { startedAt: detailClock(), mediaType, tmdbId, seasonNumber: null, reported: new Set() });
  while (detailTimings.size > 12) detailTimings.delete(detailTimings.keys().next().value as number);
}

function reportDetailMetric(
  requestId: number,
  stage: DetailMetricStage,
  status: "success" | "failed" | "discarded" | "cancelled",
  options: { cached?: boolean; errorCode?: string; once?: boolean } = {},
): void {
  const timing = detailTimings.get(requestId);
  if (!timing) return;
  if (options.once && timing.reported.has(stage)) return;
  if (options.once) timing.reported.add(stage);
  const durationMs = Math.max(0, Math.round(detailClock() - timing.startedAt));
  void api.recordMediaDetailMetric(timing.mediaType, timing.tmdbId, {
    stage,
    status,
    duration_ms: durationMs,
    cached: options.cached ?? false,
    season_number: timing.seasonNumber,
    ...(options.errorCode ? { error_code: options.errorCode } : {}),
  }).catch(() => undefined);
}

interface ResourceRouteState extends MediaResourceRouteState {}

const resourceCache = new Map<string, ResourcePageResponse>();
const seasonDetailCache = new Map<string, SeasonDetailResponse>();

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
const hasActiveTasks = computed(() => tasks.value.some(isActiveTask));
const resourceItems = computed(() => {
  const source = resourceResponse.value?.items ?? resourceItemsFallback.value;
  return source.map((resource) => {
    const inspection = inspectionResults.value.get(resource.resource_id);
    if (inspection) return mergeInspectionResult(resource, inspection);
    const status = inspectionStatusOverrides.value.get(resource.resource_id);
    return status ? { ...resource, inspection_status: status } : resource;
  });
});

function defaultResourceRoute(): ResourceRouteState {
  return { page: 1, kind: "all", quality: "all", query: "", sort: "comprehensive", pageSize: 25 };
}

function currentResourceRoute(): ResourceRouteState {
  return {
    page: resourcePage.value,
    kind: resourceKind.value,
    quality: resourceQuality.value,
    query: resourceQuery.value,
    sort: resourceSort.value,
    pageSize: resourcePageSize.value,
  };
}

function applyResourceRoute(route: Partial<ResourceRouteState> = {}) {
  const defaults = defaultResourceRoute();
  const requestedPage = Number.isInteger(route.page) && (route.page ?? 0) >= 1 ? route.page! : defaults.page;
  resourcePage.value = Math.min(Math.max(1, resourceTotalPages.value), requestedPage);
  resourceKind.value = route.kind ?? defaults.kind;
  resourceQuality.value = route.quality ?? defaults.quality;
  resourceQuery.value = (route.query ?? defaults.query).trim();
  resourceSort.value = route.sort ?? defaults.sort;
  resourcePageSize.value = route.pageSize ?? defaults.pageSize;
}

function resourceCacheKey(route: ResourceRouteState, snapshotRevision?: string | null): string {
  return [detailMediaType.value, result.value?.movie.tmdb_id ?? "", selectedSeason.value ?? "all", snapshotRevision ?? "snapshot", route.kind, route.quality, route.query.trim(), route.sort, route.page, route.pageSize].join("|");
}

function invalidateResourceRequest(): void {
  resourceAbortController?.abort();
  resourceAbortController = null;
  resourceRequestId += 1;
}

function clearResourcePagination() {
  invalidateResourceRequest();
  resourceResponse.value = null;
  resourceItemsFallback.value = [];
  resourceLoading.value = false;
  resourceSearchLoading.value = false;
  resourceError.value = "";
  resourcePaginationUnavailable.value = false;
  resourceTotal.value = 0;
  resourceHiddenTotal.value = 0;
  resourceTotalPages.value = 1;
  resourceFacets.value = { magnet: 0, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 };
  resourceCache.clear();
  pendingResourceRoute = null;
  if (resourceQueryTimer !== undefined) {
    window.clearTimeout(resourceQueryTimer);
    resourceQueryTimer = undefined;
  }
}

function applyResourceResponse(response: ResourcePageResponse, requestedPage = 1) {
  if (resourceResponse.value && resourceResponse.value.snapshot_revision !== response.snapshot_revision) resourceCache.clear();
  const safeTotalPages = Math.max(1, Math.min(500, Math.trunc(response.total_pages) || 1));
  resourceResponse.value = response;
  const responsePage = Math.trunc(response.page) || requestedPage;
  resourcePage.value = response.total === 0 ? 1 : Math.max(1, Math.min(safeTotalPages, responsePage));
  resourcePageSize.value = response.page_size;
  resourceTotal.value = response.total;
  resourceHiddenTotal.value = response.hidden_total ?? 0;
  resourceTotalPages.value = safeTotalPages;
  resourceFacets.value = response.facets;
  resourcePaginationUnavailable.value = false;
  resourceError.value = "";
}

function resourceRouteFromMediaRoute(mediaRoute: ReturnType<typeof extractMediaRoute>): ResourceRouteState {
  return {
    page: mediaRoute?.resourcePage ?? 1,
    kind: mediaRoute?.resourceKind ?? "all",
    quality: mediaRoute?.resourceQuality ?? "all",
    query: mediaRoute?.resourceQuery ?? "",
    sort: mediaRoute?.resourceSort ?? "comprehensive",
    pageSize: mediaRoute?.resourcePageSize ?? 25,
  };
}

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

interface CatalogReturnState {
  route: CatalogRoute;
  scrollY: number;
  backDelta: number;
}

function readCatalogReturnState(): CatalogReturnState | null {
  const state = window.history.state;
  const value = state?.catalog;
  if (state?.catalogDetailEntry !== true || !value || typeof value !== "object") return null;
  const candidate = value as Partial<CatalogRoute>;
  const validView = candidate.view === "movies" || candidate.view === "tv" || candidate.view === "popular" || candidate.view === "search";
  const validSort = candidate.sort === "popular" || candidate.sort === "rating" || candidate.sort === "release";
  const validPage = Number.isInteger(candidate.page) && candidate.page >= 1 && candidate.page <= 500;
  const validGenre = candidate.genreId === undefined || (Number.isInteger(candidate.genreId) && candidate.genreId >= 1);
  const validYear = candidate.year === undefined || (Number.isInteger(candidate.year) && candidate.year >= 1900 && candidate.year <= 2100);
  const scrollY = state.catalogScrollY;
  const backDelta = state.catalogBackDelta;
  if (!validView || !validSort || !validPage || !validGenre || !validYear
    || typeof scrollY !== "number" || !Number.isFinite(scrollY) || scrollY < 0
    || !Number.isInteger(backDelta) || backDelta < 1 || backDelta > Math.max(0, window.history.length - 1)) return null;
  const route = normalizeCatalogRoute({
    view: candidate.view,
    query: typeof candidate.query === "string" ? candidate.query : "",
    page: candidate.page as number,
    sort: candidate.sort,
    ...(candidate.genreId !== undefined ? { genreId: candidate.genreId as number } : {}),
    ...(candidate.year !== undefined ? { year: candidate.year as number } : {}),
  });
  return { route, scrollY, backDelta };
}

function restoreCatalogReturnState(): void {
  const restored = readCatalogReturnState();
  if (!restored) {
    catalogReturnRoute = null;
    catalogReturnScrollY = null;
    return;
  }
  catalogReturnRoute = restored.route;
  catalogReturnScrollY = restored.scrollY;
  previousView.value = restored.route.view;
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
  catalogError.value = "";
  failedCatalogRoute = null;
  error.value = "";

  const cached = readCatalogCache(safeRoute);
  if (cached) {
    if (!isCurrent()) return;
    const committedRoute = { ...safeRoute, page: cached.page };
    applyCatalogData(committedRoute, cached);
    pendingCatalogRoute = null;
    catalogLoading.value = false;
    catalogError.value = "";
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
    catalogError.value = "";
    commitCatalogRoute(committedRoute, historyMode, restoreY ?? 0);
    catalogScroll(restoreY);
  } catch (exception) {
    if (!isCurrent()) return;
    pendingCatalogRoute = null;
    catalogLoading.value = false;
    failedCatalogRoute = safeRoute;
    catalogError.value = exception instanceof ApiError ? exception.message : "目录加载失败，请稍后重试";
    // Keep an initial failed route visible so an upstream failure is not shown as an empty result.
    if (!committedCatalogRoute.value) applyCatalogPreview(safeRoute);
  }
}

async function retryCatalog() {
  if (!failedCatalogRoute || catalogLoading.value) return;
  await requestCatalog(failedCatalogRoute, "replace");
}
function inspectionBatchIds(limit = 8, retriesOnly = false): string[] {
  if (!result.value || !resourceItems.value.length) return [];
  const unavailableIds = new Set([...inspectionProcessedIds.value, ...inspectionInFlightIds.value]);
  const candidates = nextInspectionResourceIds(
    resourceItems.value,
    unavailableIds,
    30,
  );
  const eligible = retriesOnly
    ? candidates.filter((resourceId) => inspectionRetryIds.value.has(resourceId))
    : candidates.filter((resourceId) => !inspectionRetryIds.value.has(resourceId));
  if (retriesOnly) return eligible.slice(0, limit);
  const remainingNewIds = Math.max(0, MAX_INSPECTABLE_MAGNETS - inspectionSeenIds.value.size);
  return eligible.filter((resourceId) => !inspectionSeenIds.value.has(resourceId)).slice(0, Math.min(limit, remainingNewIds));
}

const inspectionRetryAvailable = computed(() => inspectionBatchIds(8, true).length > 0);
const inspectionMoreAvailable = computed(() => {
  if (!inspectionSupported.value || !result.value || inspectionState.value === "running") return false;
  if (inspectionAutoRequestId.value !== searchRequestId) return false;
  return inspectionBatchIds(1).length > 0;
});

const navItems = [
  { view: "workbench" as const, label: "工作台", icon: LayoutDashboard },
  { view: "home" as const, label: "首页", icon: Home },
  { view: "movies" as const, label: "电影", icon: Film },
  { view: "tv" as const, label: "剧集", icon: Tv },
  { view: "popular" as const, label: "热门", icon: Flame },
  { view: "favorites" as const, label: "收藏", icon: Heart },
  { view: "history" as const, label: "记录", icon: Clock3 },
  { view: "organization-plans" as const, label: "整理", icon: ClipboardCheck },
  { view: "organization-history" as const, label: "整理历史", icon: Clock3 },
  { view: "library" as const, label: "媒体库", icon: Database },
];

const navGroups = [
  { label: "主线", items: navItems.slice(0, 1) },
  { label: "发现", items: navItems.slice(1, 7) },
  { label: "入库", items: navItems.slice(7) },
];

function healthCapability(
  enabled: boolean | undefined,
  reasonCode: string,
  label: string,
  settingsSection: CapabilityAvailability["settings_section"] = "overview",
): CapabilityAvailability {
  if (enabled === undefined) return unavailableCapability(settingsSection);
  return {
    enabled,
    reason_code: enabled ? null : reasonCode,
    reason_zh: enabled ? "可执行" : `${label}未启用，请在${settingsSection === "organization" ? "自动整理设置" : "设置概览"}查看功能状态。`,
    settings_section: settingsSection,
  };
}

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
  homeError.value = "";
  try {
    homeCatalog.value = await api.homeCatalog();
  } catch (exception) {
    homeError.value = exception instanceof ApiError ? exception.message : "首页资料暂不可用，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function retryHome() {
  if (catalogLoading.value) return;
  await loadHome();
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

async function searchFromWorkbench(value: string): Promise<void> {
  searchInput.value = value;
  await performSearch(true);
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
  if ((view === "organization-plans" || view === "organization-history") && !organizationPlanEnabled.value) {
    await selectView("home");
    return;
  }
  invalidateDetailRequest();
  result.value = null;
  error.value = "";
  catalogError.value = "";
  failedCatalogRoute = null;
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

function navigateFromTaskDrawer(view: "library" | "settings" | "workflows") {
  drawerOpen.value = false;
  void selectView(view);
}

function navigateFromWorkbench(
  view: Exclude<BrowseView, "search">,
  settingsSection: "overview" | "organization" = "overview",
): void {
  settingsInitialSection.value = settingsSection;
  void selectView(view);
}

function openTaskDrawer(): void {
  drawerOpen.value = true;
}

function updateTask(task: TaskResponse): void {
  taskRefreshGuard.invalidate(task.id);
  tasks.value = [task, ...tasks.value.filter((item) => item.id !== task.id)].slice(0, 50);
  if (isActiveTask(task)) ensurePolling();
}

function replaceTasks(loaded: TaskResponse[]): void {
  tasks.value = loaded.slice(0, 50);
  if (tasks.value.some(isActiveTask)) ensurePolling();
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
  inspectionSeenIds.value = new Set();
  inspectionResults.value = new Map();
  inspectionStatusOverrides.value = new Map();
  inspectionAutoRequestId.value = null;
}

function invalidateDetailRequest() {
  if (
    (metadataAbortController && !metadataAbortController.signal.aborted)
    || (resourceSearchAbortController && !resourceSearchAbortController.signal.aborted)
  ) {
    const previous = [...detailTimings.entries()].at(-1);
    if (previous) reportDetailMetric(previous[0], "request_cancelled", "cancelled", { once: true });
  }
  searchRequestId += 1;
  metadataRequestId += 1;
  metadataAbortController?.abort();
  metadataAbortController = null;
  resourceSearchAbortController?.abort();
  resourceSearchAbortController = null;
  metadataLoading.value = false;
  metadataError.value = "";
  metadataStale.value = false;
  invalidateSeasonDetailRequest();
  catalogRequestId += 1;
  pendingCatalogRoute = null;
  catalogLoading.value = false;
  loading.value = false;
  clearResourcePagination();
  resetInspection();
  activeWorkflowId.value = null;
}

async function ensureActiveWorkflow(resourceId?: string): Promise<string | null> {
  if (activeWorkflowId.value) return activeWorkflowId.value;
  if (!result.value || !result.value.movie.media_type) return null;
  if (workflowCreationPromise) return workflowCreationPromise;
  const mediaType = detailMediaType.value;
  const tmdbId = result.value.movie.tmdb_id;
  workflowCreationPromise = (async () => {
    try {
      const workflow = await api.createWorkflow({ mediaType, tmdbId, resourceId });
      if (result.value?.movie.tmdb_id !== tmdbId || detailMediaType.value !== mediaType) return null;
      activeWorkflowId.value = workflow.id;
      return workflow.id;
    } catch {
      return null;
    } finally {
      workflowCreationPromise = null;
    }
  })();
  return workflowCreationPromise;
}

function detailResult(movie: MovieMetadata): SearchResponse {
  return {
    movie,
    results: [],
    warnings: [],
    cached: false,
    cache_age_seconds: null,
  };
}

function detailPlaceholder(tmdbId: number, mediaType: "movie" | "tv"): MovieMetadata {
  return {
    tmdb_id: tmdbId,
    media_type: mediaType,
    title: "正在加载影视资料",
    original_title: null,
    release_year: null,
    overview: null,
    poster_path: null,
    backdrop_path: null,
    genre_ids: [],
    vote_average: null,
    seasons: [],
  };
}

function isMovieMetadata(value: unknown): value is MovieMetadata {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<MovieMetadata>;
  return typeof candidate.tmdb_id === "number"
    && (candidate.media_type === "movie" || candidate.media_type === "tv")
    && typeof candidate.title === "string";
}

function isResourceSearchResponse(value: unknown): value is ResourceSearchResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ResourceSearchResponse>;
  return typeof candidate.task_id === "string"
    && ["queued", "running", "ready", "failed"].includes(candidate.status ?? "");
}

async function loadMetadata(
  tmdbId: number,
  mediaType: "movie" | "tv",
  initialMovie?: MovieMetadata,
): Promise<void> {
  const detailRequest = searchRequestId;
  const requestId = ++metadataRequestId;
  metadataAbortController?.abort();
  const controller = new AbortController();
  metadataAbortController = controller;
  metadataLoading.value = true;
  metadataError.value = "";
  metadataStale.value = false;
  if (initialMovie && (!result.value || result.value.movie.tmdb_id !== tmdbId)) {
    result.value = detailResult(initialMovie);
  }
  try {
    const movie = await api.mediaMetadata(mediaType, tmdbId, controller.signal);
    if (!isMovieMetadata(movie)) throw new ApiError("影视资料响应格式无效", 502, "tmdb_unavailable");
    if (requestId !== metadataRequestId || controller.signal.aborted || !result.value || result.value.movie.tmdb_id !== tmdbId) {
      if (!controller.signal.aborted) reportDetailMetric(detailRequest, "late_response", "discarded");
      return;
    }
    result.value = { ...result.value, movie };
    metadataError.value = "";
    reportDetailMetric(detailRequest, "metadata_complete", "success", { once: true });
  } catch (exception) {
    if (requestId !== metadataRequestId || controller.signal.aborted) return;
    metadataError.value = exception instanceof ApiError ? exception.message : "影视资料暂时无法加载";
    metadataStale.value = Boolean(initialMovie || result.value?.movie.title !== "正在加载影视资料");
    reportDetailMetric(detailRequest, "metadata_failed", "failed", { errorCode: exception instanceof ApiError ? exception.code : "metadata_unavailable", once: true });
  } finally {
    if (requestId === metadataRequestId) metadataLoading.value = false;
  }
}

function invalidateSeasonDetailRequest() {
  seasonDetailRequestId += 1;
  seasonDetailAbortController?.abort();
  seasonDetailAbortController = null;
  seasonDetailLoading.value = false;
  seasonDetailError.value = "";
}

function seasonDetailKey(tmdbId: number, seasonNumber: number): string {
  return `${tmdbId}:${seasonNumber}`;
}

async function loadSeasonDetail(
  tmdbId: number,
  seasonNumber: number | null,
  refresh = false,
  detailRequestId = searchRequestId,
): Promise<void> {
  invalidateSeasonDetailRequest();
  seasonDetail.value = null;
  if (seasonNumber === null || detailRequestId !== searchRequestId) return;
  const requestId = seasonDetailRequestId;
  const key = seasonDetailKey(tmdbId, seasonNumber);
  const cached = seasonDetailCache.get(key);
  if (cached && !refresh) seasonDetail.value = cached;
  seasonDetailLoading.value = !cached || refresh;
  const controller = new AbortController();
  seasonDetailAbortController = controller;
  try {
    if (cached && !refresh) {
      seasonDetailLoading.value = false;
      return;
    }
    const response = await api.seasonMetadata(tmdbId, seasonNumber, { refresh }, controller.signal);
    if (requestId !== seasonDetailRequestId || detailRequestId !== searchRequestId || controller.signal.aborted || selectedSeason.value !== seasonNumber) return;
    seasonDetailCache.set(key, response);
    seasonDetail.value = response;
    seasonDetailError.value = "";
  } catch (exception) {
    if (requestId !== seasonDetailRequestId || controller.signal.aborted) return;
    seasonDetailError.value = exception instanceof ApiError ? exception.message : "季度资料加载失败，请重试";
  } finally {
    if (requestId === seasonDetailRequestId) seasonDetailLoading.value = false;
  }
}

function beginResourceSnapshot(fallback: ResourceSummary[], hiddenTotal = 0, preserveExisting = false) {
  invalidateResourceRequest();
  if (!preserveExisting) {
    resourceResponse.value = null;
    resourceItemsFallback.value = fallback;
    resourceLoading.value = false;
  }
  resourceError.value = "";
  resourcePaginationUnavailable.value = false;
  if (!preserveExisting) {
    resourceTotal.value = fallback.length;
    resourceHiddenTotal.value = hiddenTotal;
    resourceTotalPages.value = 1;
    resourceFacets.value = {
      magnet: fallback.filter((item) => item.kind === "magnet").length,
      share: fallback.filter((item) => item.kind === "115_share").length,
      "4k": 0,
      "1080p": 0,
      "720p": 0,
      subtitle: 0,
    };
  }
  resourceCache.clear();
  pendingResourceRoute = null;
}

async function loadResourcePage(route: ResourceRouteState, historyMode: "push" | "replace" | "none" = "push", correctionAttempted = false): Promise<void> {
  if (!result.value) return;
  const detailRequest = searchRequestId;
  const requestId = ++resourceRequestId;
  resourceAbortController?.abort();
  const controller = new AbortController();
  resourceAbortController = controller;
  const searchId = searchRequestId;
  const safeRoute: ResourceRouteState = {
    ...route,
    page: Math.max(1, Math.min(500, Math.trunc(route.page) || 1)),
    query: route.query.trim(),
  };
  const isCurrent = () => requestId === resourceRequestId && searchId === searchRequestId && !controller.signal.aborted && !!result.value;
  const cached = resourceCache.get(resourceCacheKey(safeRoute, resourceResponse.value?.snapshot_revision ?? null));
  resourceLoading.value = true;
  resourceError.value = "";
  if (cached) {
    if (!isCurrent()) {
      if (!controller.signal.aborted) reportDetailMetric(detailRequest, "late_response", "discarded");
      return;
    }
    applyResourceResponse(cached, safeRoute.page);
    reportDetailMetric(detailRequest, "resource_first_batch", "success", { cached: true, once: true });
    reportDetailMetric(detailRequest, "resource_complete", "success", { cached: true, once: true });
    applyResourceRoute(safeRoute);
    pendingResourceRoute = null;
    resourceLoading.value = false;
    if (historyMode !== "none") navigateToMedia(detailMediaType.value, result.value.movie.tmdb_id, detailMediaType.value === "tv" ? selectedSeason.value ?? undefined : undefined, safeRoute, historyMode === "replace");
    startAutomaticInspection(searchId);
    return;
  }
  try {
    const response = await api.resources(detailMediaType.value, result.value.movie.tmdb_id, {
      seasonNumber: detailMediaType.value === "tv" ? selectedSeason.value : undefined,
      kind: safeRoute.kind === "all" ? undefined : safeRoute.kind,
      quality: safeRoute.quality === "all" ? undefined : safeRoute.quality,
      query: safeRoute.query,
      sort: safeRoute.sort,
      page: safeRoute.page,
      pageSize: safeRoute.pageSize,
    }, controller.signal);
    if (!isCurrent()) {
      if (!controller.signal.aborted) reportDetailMetric(detailRequest, "late_response", "discarded");
      return;
    }
    const actualTotalPages = Math.max(1, Math.min(500, Math.trunc(response.total_pages) || 1));
    const legalPage = response.total === 0 ? 1 : actualTotalPages;
    if (safeRoute.page > legalPage && !correctionAttempted) {
      await loadResourcePage({ ...safeRoute, page: legalPage }, "replace", true);
      return;
    }
    const actualPage = response.total === 0 ? 1 : Math.max(1, Math.min(actualTotalPages, Math.trunc(response.page) || safeRoute.page));
    resourceCache.set(resourceCacheKey({ ...safeRoute, page: actualPage }, response.snapshot_revision), response);
    while (resourceCache.size > 20) resourceCache.delete(resourceCache.keys().next().value as string);
    applyResourceResponse(response, actualPage);
    reportDetailMetric(detailRequest, "resource_first_batch", "success", { cached: resourceSearchCached, once: true });
    reportDetailMetric(detailRequest, "resource_complete", "success", { cached: resourceSearchCached, once: true });
    applyResourceRoute({ ...safeRoute, page: actualPage });
    pendingResourceRoute = null;
    resourcePage.value = actualPage;
    resourceLoading.value = false;
    if (historyMode !== "none") navigateToMedia(detailMediaType.value, result.value.movie.tmdb_id, detailMediaType.value === "tv" ? selectedSeason.value ?? undefined : undefined, { ...safeRoute, page: actualPage }, historyMode === "replace");
    startAutomaticInspection(searchId);
  } catch (exception) {
    if (!isCurrent()) {
      if (!controller.signal.aborted) reportDetailMetric(detailRequest, "late_response", "discarded");
      return;
    }
    resourceLoading.value = false;
    if (exception instanceof ApiError && exception.status === 404 && exception.code === "resource_snapshot_not_found") {
      pendingResourceRoute = null;
      resourcePaginationUnavailable.value = true;
      resourceError.value = "资源分页暂不可用，当前显示搜索快照结果";
      resourcePage.value = 1;
      resourceTotalPages.value = 1;
      startAutomaticInspection(searchId);
      return;
    }
    pendingResourceRoute = null;
    resourceError.value = exception instanceof ApiError ? exception.message : "资源分页加载失败，请重试";
    reportDetailMetric(detailRequest, "resource_failed", "failed", { errorCode: exception instanceof ApiError ? exception.code : "resource_page_failed", once: true });
  }
}

function changeResourcePage(page: number) {
  if (resourceLoading.value) return;
  const target = Math.max(1, Math.min(resourceTotalPages.value, Math.trunc(page)));
  if (target === resourcePage.value) return;
  void loadResourcePage({ ...currentResourceRoute(), page: target });
}

function changeResourceFilter(next: Partial<ResourceRouteState>) {
  const route = { ...(pendingResourceRoute ?? currentResourceRoute()), ...next, page: 1 };
  if (route.kind === resourceKind.value && route.quality === resourceQuality.value && route.sort === resourceSort.value && route.pageSize === resourcePageSize.value && route.query === resourceQuery.value) return;
  pendingResourceRoute = route;
  void loadResourcePage(route);
}

function changeResourceQuery(value: string) {
  if (resourceQueryTimer !== undefined) window.clearTimeout(resourceQueryTimer);
  invalidateResourceRequest();
  resourceQueryTimer = window.setTimeout(() => {
    const nextQuery = value.trim();
    if (nextQuery === resourceQuery.value) {
      pendingResourceRoute = null;
      if (resourceResponse.value) {
        resourceLoading.value = false;
        resourceError.value = "";
      } else {
        void loadResourcePage(currentResourceRoute(), "replace");
      }
    } else {
      changeResourceFilter({ query: nextQuery });
    }
    resourceQueryTimer = undefined;
  }, 250);
}

async function refreshResources() {
  if (!result.value) return;
  await loadResources(result.value.movie.tmdb_id, detailMediaType.value, true, selectedSeason.value, false, currentResourceRoute());
}

async function loadResources(
  id: number,
  mediaType: "movie" | "tv",
  refresh = false,
  seasonNumber: number | null = selectedSeason.value,
  clearResult = false,
  initialResourceRoute: ResourceRouteState = defaultResourceRoute(),
) {
  const requestId = searchRequestId;
  const preserveResourceSnapshot = !clearResult && resourceItems.value.length > 0;
  if (resourceSearchAbortController && !resourceSearchAbortController.signal.aborted) {
    reportDetailMetric(requestId, "request_cancelled", "cancelled", { once: true });
  }
  resourceSearchAbortController?.abort();
  const controller = new AbortController();
  resourceSearchAbortController = controller;
  const isCurrentSearch = () => requestId === searchRequestId
    && resourceSearchAbortController === controller
    && !controller.signal.aborted
    && !!result.value
    && result.value.movie.tmdb_id === id;
  resetInspection();
  detailMediaType.value = mediaType;
  if (clearResult) {
    clearResourcePagination();
  }
  resourceSearchLoading.value = true;
  resourceError.value = "";
  resourceSearchCached = false;
  try {
    const task = await api.startResourceSearch(mediaType, id, {
      refresh,
      seasonNumber: mediaType === "tv" ? seasonNumber : null,
    }, controller.signal);
    if (!isResourceSearchResponse(task)) {
      throw new ApiError("资源搜索任务接口不可用", 404, "resource_search_endpoint_unavailable");
    }
    const response = await pollResourceSearch(api, task, {
      requestId,
      currentRequestId: () => searchRequestId,
      signal: controller.signal,
    });
    if (!isCurrentSearch() || !response) {
      if (requestId !== searchRequestId) reportDetailMetric(requestId, "late_response", "discarded");
      return;
    }
    if (response.status === "failed") {
      resourceLoading.value = false;
      // Do not keep serving the previous snapshot: the user asked to refresh
      // and the search failed, so stale rows would be presented as current.
      beginResourceSnapshot([], 0, false);
      resourceError.value = describeUiError(response.error_code ?? "resource_search_failed", 502).message;
      reportDetailMetric(requestId, "resource_failed", "failed", { errorCode: response.error_code ?? "resource_search_failed", once: true });
      return;
    }
    const cacheAgeSeconds = response.cache_age_seconds;
    result.value = {
      ...result.value,
      warnings: response.warnings,
      cached: cacheAgeSeconds !== null,
      cache_age_seconds: cacheAgeSeconds,
    };
    searchSourceNames.value = sourceNameList(response.sources ?? []);
    resourceSearchCached = response.cache_age_seconds !== null;
    reportDetailMetric(requestId, "resource_first_batch", "success", { cached: resourceSearchCached, once: true });
    selectedSeason.value = mediaType === "tv" ? response.selected_season ?? seasonNumber : null;
    const timing = detailTimings.get(requestId);
    if (timing) timing.seasonNumber = selectedSeason.value;
    void loadSeasonDetail(id, selectedSeason.value, refresh, requestId);
    applyResourceRoute(initialResourceRoute);
    beginResourceSnapshot([], 0, preserveResourceSnapshot);
    void loadResourcePage(initialResourceRoute, "none");
  } catch (exception) {
    if (controller.signal.aborted || resourceSearchAbortController !== controller) return;
    if (exception instanceof ApiError && [404, 405, 501, 503].includes(exception.status)) {
      try {
        const legacy = await api.search(id, mediaType, refresh, mediaType === "tv" ? seasonNumber ?? undefined : undefined);
        if (!isCurrentSearch()) return;
        result.value = {
          ...result.value,
          movie: legacy.movie,
          results: legacy.results,
          warnings: legacy.warnings,
          cached: legacy.cached,
          cache_age_seconds: legacy.cache_age_seconds,
          selected_season: legacy.selected_season,
          hidden_total: legacy.hidden_total,
        };
        searchSourceNames.value = sourceNameList(legacy.results.map((resource) => resource.source));
        selectedSeason.value = mediaType === "tv" ? legacy.selected_season ?? seasonNumber : null;
        resourceSearchCached = legacy.cached;
        applyResourceRoute(initialResourceRoute);
        beginResourceSnapshot(legacy.results, legacy.hidden_total ?? 0, preserveResourceSnapshot);
        await loadResourcePage(initialResourceRoute, "none");
        return;
      } catch (legacyException) {
        if (legacyException instanceof DOMException && legacyException.name === "AbortError") return;
        exception = legacyException;
      }
    }
    if (requestId === searchRequestId) {
      resourceLoading.value = false;
      resourceError.value = exception instanceof ApiError ? exception.message : "可用资源暂时无法加载";
      reportDetailMetric(requestId, "resource_failed", "failed", { errorCode: exception instanceof ApiError ? exception.code : "resource_search_failed", once: true });
    }
  } finally {
    if (resourceSearchAbortController === controller) {
      resourceSearchAbortController = null;
      resourceSearchLoading.value = false;
    }
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
  invalidateDetailRequest();
  startDetailTiming(searchRequestId, mediaTypeOf(movie), movie.tmdb_id);
  result.value = detailResult(movie);
  searchSourceNames.value = [];
  selectedSeason.value = null;
  seasonDetail.value = null;
  detailMediaType.value = mediaTypeOf(movie);
  const mediaType = mediaTypeOf(movie);
  beginResourceSnapshot([]);
  reportDetailMetric(searchRequestId, "detail_framework", "success", { once: true });
  reportDetailMetric(searchRequestId, "metadata_summary", "success", { cached: true, once: true });
  navigateToMedia(mediaType, movie.tmdb_id, undefined, defaultResourceRoute());
  if (catalogReturnRoute) {
    window.history.replaceState({
      ...window.history.state,
      catalog: catalogReturnRoute,
      catalogScrollY: catalogReturnScrollY,
      catalogDetailEntry: true,
      catalogBackDelta: 1,
    }, "", window.location.href);
  }
  void loadMetadata(movie.tmdb_id, mediaType, movie);
  void loadResources(movie.tmdb_id, mediaType, false, null, false, defaultResourceRoute());
}

async function selectSeason(seasonNumber: number | null) {
  if (!result.value || detailMediaType.value !== "tv") return;
  const movie = result.value.movie;
  selectedSeason.value = seasonNumber;
  const timing = detailTimings.get(searchRequestId);
  if (timing) timing.seasonNumber = seasonNumber;
  seasonDetail.value = null;
  const nextRoute = { ...currentResourceRoute(), page: 1, kind: "all" as const, quality: "all" as const, query: "" };
  navigateToMedia("tv", movie.tmdb_id, seasonNumber ?? undefined, nextRoute, true);
  beginResourceSnapshot([]);
  void loadResources(movie.tmdb_id, "tv", false, seasonNumber, false, nextRoute);
}

async function returnToBrowse() {
  invalidateDetailRequest();
  result.value = null;
  searchSourceNames.value = [];
  const persistedReturnState = readCatalogReturnState();
  if (catalogReturnRoute && persistedReturnState) {
    catalogReturnRoute = null;
    catalogReturnScrollY = null;
    window.history.go(-persistedReturnState.backDelta);
    return;
  }
  const view = previousView.value === "search" ? "search" : previousView.value;
  activeView.value = view;
  navigateToView(view);
  await loadView(view);
}

async function initializeWorkspace() {
  if ((window.location.pathname === "/organization-plans" || window.location.pathname === "/organization-history") && !organizationPlanEnabled.value) {
    activeView.value = "home";
    previousView.value = "home";
    navigateToView("home", true);
    await loadHome();
    return;
  }
  const mediaRoute = extractMediaRoute(window.location.pathname + window.location.search);
  if (mediaRoute !== null) {
    restoreCatalogReturnState();
    invalidateDetailRequest();
    selectedSeason.value = mediaRoute.mediaType === "tv" ? mediaRoute.seasonNumber ?? null : null;
    detailMediaType.value = mediaRoute.mediaType;
    startDetailTiming(searchRequestId, mediaRoute.mediaType, mediaRoute.tmdbId);
    const timing = detailTimings.get(searchRequestId);
    if (timing) timing.seasonNumber = selectedSeason.value;
    result.value = detailResult(detailPlaceholder(mediaRoute.tmdbId, mediaRoute.mediaType));
    beginResourceSnapshot([]);
    reportDetailMetric(searchRequestId, "detail_framework", "success", { once: true });
    const resourceRoute = resourceRouteFromMediaRoute(mediaRoute);
    void loadMetadata(mediaRoute.tmdbId, mediaRoute.mediaType);
    void loadResources(mediaRoute.tmdbId, mediaRoute.mediaType, false, selectedSeason.value, false, resourceRoute);
    return;
  }
  const catalogRoute = parseCatalogRoute(window.location.pathname + window.location.search);
  if (catalogRoute) {
    if (catalogRoute.view === "search" && !catalogRoute.query.trim()) {
      activeView.value = "home";
      previousView.value = "home";
      navigateToView("home", true);
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
    await api.login(username.value, password.value);
    authenticated.value = true;
    password.value = "";
    await initializeWorkspace();
  } catch (exception) {
    focusFirstFieldError(exception);
    error.value = exception instanceof ApiError ? "账号或密码不正确" : "登录失败";
  }
}

async function startPush(resource: ResourceSummary) {
  if (!canPushResource(resource, pushCapabilities.value)) {
    error.value = resource.kind === "115_share" ? "115 分享转存尚未验证" : "磁力云下载不可用";
    return;
  }
  try {
    const settings = await api.organizationSettings();
    if (!settings.push_directory_id) {
      error.value = "请先在设置的“资源推送目录”中选择目录并保存。";
      return;
    }
    void submitPush(resource, settings.push_directory_id);
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "推送目录读取失败，请前往设置检查目录配置。";
  }
}

async function submitPush(resource: ResourceSummary, targetDirectoryId: string) {
  pushingId.value = resource.resource_id;
  error.value = "";
  try {
    const workflowId = await ensureActiveWorkflow(resource.resource_id);
    const task = await submitPushResource(resource, pushCapabilities.value, (resourceId) => api.createTask(resourceId, false, workflowId, targetDirectoryId));
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
  const cachedResults = new Map(inspectionResults.value);
  const statusOverrides = new Map(inspectionStatusOverrides.value);
  response.results.forEach((item) => {
    cachedResults.set(item.resource_id, item);
    if (inspectionResultEnded(item)) statusOverrides.delete(item.resource_id);
  });
  inspectionResults.value = cachedResults;
  inspectionStatusOverrides.value = statusOverrides;
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
  const statusOverrides = new Map(inspectionStatusOverrides.value);
  resourceIds.forEach((resourceId) => statusOverrides.set(resourceId, status as ResourceSummary["inspection_status"]));
  inspectionStatusOverrides.value = statusOverrides;
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
  const statusOverrides = new Map(inspectionStatusOverrides.value);
  resourceIds.forEach((resourceId) => statusOverrides.set(resourceId, status));
  inspectionStatusOverrides.value = statusOverrides;
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
  inspectionSeenIds.value = new Set([...inspectionSeenIds.value, ...resourceIds]);
  inspectionInFlightIds.value = new Set([...inspectionInFlightIds.value, ...resourceIds]);
  inspectionState.value = "running";
  inspectionCompleted.value = 0;
  inspectionTotal.value = 0;
  inspectionFailed.value = 0;
  inspectionError.value = null;
  setInspectionStatus(resourceIds, "running");
  const isCurrent = () => requestId === searchRequestId && runId === inspectionRunId;

  try {
    const workflowId = await ensureActiveWorkflow(resourceIds[0]);
    const started = await api.inspectResources(resourceIds, workflowId);
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
  if (inspectionAutoStartEnabled.value !== true || !inspectionSupported.value || !result.value) return;
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
    const active = tasks.value.filter(isActiveTask);
    const updates = await Promise.all(active.map(async (task) => {
      const version = taskRefreshGuard.begin(task.id);
      try {
        const response = await api.getTask(task.id);
        return taskRefreshGuard.isCurrent(task.id, version) ? response : null;
      } catch {
        return null;
      }
    }));
    const byId = new Map(
      updates.filter((task): task is TaskResponse => task !== null).map((task) => [task.id, task]),
    );
    tasks.value = tasks.value.map((task) => byId.get(task.id) ?? task);
  }, 2000);
}

async function syncRoute() {
  const mediaRoute = extractMediaRoute(window.location.pathname + window.location.search);
  const routeSeason = mediaRoute?.mediaType === "tv" ? mediaRoute.seasonNumber ?? null : null;
  if (mediaRoute && result.value
    && mediaRoute.tmdbId === result.value.movie.tmdb_id
    && mediaRoute.mediaType === detailMediaType.value
    && routeSeason === selectedSeason.value) {
    selectedSeason.value = mediaRoute.mediaType === "tv" ? mediaRoute.seasonNumber ?? null : null;
    const route = resourceRouteFromMediaRoute(mediaRoute);
    applyResourceRoute(route);
    pendingResourceRoute = null;
    await loadResourcePage(route, "none");
    return;
  }
  invalidateDetailRequest();
  result.value = null;
  await initializeWorkspace();
}

function updateOnline(): void {
  isOnline.value = browserIsOnline();
}

function openOrganizationSettings(section: "overview" | "organization" = "organization"): void {
  settingsInitialSection.value = section;
  void selectView("settings");
}

function recordOfflineData(event: Event): void {
  const cachedAt = (event as CustomEvent<{ cachedAt?: string }>).detail?.cachedAt;
  if (cachedAt) offlineDataAt.value = cachedAt;
}

onMounted(async () => {
  window.addEventListener("online", updateOnline);
  window.addEventListener("offline", updateOnline);
  window.addEventListener("watch-assistant:offline-data", recordOfflineData);
  try {
    const health = await api.health();
    pushCapabilities.value = resolvePushCapabilities(health);
    inspectionSupported.value = health.inspection_supported === true;
    inspectionAutoStartEnabled.value = health.inspection_auto_start_enabled === true;
    organizationPlanCapability.value = healthCapability(health.organization_plan_enabled, "organization_plan_disabled", "自动整理计划", "organization");
    organizationPlanEnabled.value = organizationPlanCapability.value.enabled;
    organizationExecutionSupported.value = health.organization_execution_supported === true;
    strmFullCapability.value = healthCapability(health.strm_capabilities?.full, "strm_full_disabled", "STRM 全量生成");
    strmIncrementalCapability.value = healthCapability(health.strm_capabilities?.incremental, "strm_incremental_disabled", "STRM 增量同步");
    const strmCleanup = health.strm_capabilities?.cleanup_capability;
    strmCleanupCapability.value = strmCleanup ?? {
      enabled: health.strm_capabilities?.cleanup === true,
      reason_code: health.strm_capabilities?.cleanup === true ? null : "strm_cleanup_disabled",
      reason_zh: health.strm_capabilities?.cleanup === true
        ? "可用"
        : "STRM 失效清理未启用，请检查部署功能开关。",
      settings_section: "overview",
    };
    emptyDirectoryCleanupCapability.value = health.organization_capabilities?.empty_directory_cleanup
      ?? unavailableCapability();
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
  window.removeEventListener("online", updateOnline);
  window.removeEventListener("offline", updateOnline);
  window.removeEventListener("watch-assistant:offline-data", recordOfflineData);
});
</script>

<template>
  <main class="app-shell">
    <header class="topbar" :class="{ authenticated }">
      <a class="brand" href="/">WATCH<span>/</span>ASSISTANT</a>
      <template v-if="authenticated">
        <nav class="primary-nav" aria-label="主导航">
          <div v-for="group in navGroups" :key="group.label" class="primary-nav-group" :aria-label="group.label"><span class="primary-nav-group-label">{{ group.label }}</span><template v-for="item in group.items" :key="item.view"><button v-if="(item.view !== 'organization-plans' && item.view !== 'organization-history') || organizationPlanEnabled" type="button" :class="{ active: activeView === item.view && !result }" @click="selectView(item.view)"><component :is="item.icon" :size="16" />{{ item.label }}</button></template></div>
        </nav>
        <form class="top-search" role="search" @submit.prevent="searchMovies"><Search :size="17" /><input v-model="searchInput" type="search" aria-label="搜索电影或电视剧" placeholder="搜索电影或电视剧" /><button type="submit" aria-label="提交搜索" title="搜索"><Search :size="17" /></button></form>
        <div class="topbar-actions" aria-label="工作流入口">
          <button class="icon-button topbar-quick-action" type="button" title="推送任务" aria-label="推送任务" @click="openTaskDrawer"><PanelRight :size="18" /><span>推送任务</span></button>
          <button class="icon-button topbar-quick-action" type="button" :class="{ active: activeView === 'workflows' && !result }" title="任务中心" aria-label="任务中心" @click="selectView('workflows')"><ListTodo :size="18" /><span>任务中心</span></button>
          <button class="icon-button topbar-quick-action" type="button" :class="{ active: activeView === 'notifications' && !result }" title="通知" aria-label="通知" @click="selectView('notifications')"><Bell :size="18" /><span>通知</span></button>
          <button class="icon-button topbar-quick-action" type="button" :class="{ active: activeView === 'settings' && !result }" title="设置" aria-label="设置" @click="selectView('settings')"><Settings :size="18" /><span>设置</span></button>
        </div>
      </template>
      <div v-else class="topbar-meta"><span class="status-dot" />局域网工作台</div>
    </header>
    <p v-if="!isOnline" class="offline-strip" role="status">当前处于离线状态，仅显示最近一次只读摘要；写操作已暂停。</p>
    <p v-else-if="offlineDataAt" class="offline-data-strip" role="status">网络已恢复，之前显示过离线缓存（{{ new Date(offlineDataAt).toLocaleString('zh-CN') }}）。</p>

    <section v-if="!authenticated" class="auth-gate"><div class="auth-mark"><LogIn :size="20" /></div><p class="eyebrow">私有工作区</p><h1>进入观影工作台</h1><p>你的 PanSou 聚合和 115 推送只在本地网络可见。</p><form @submit.prevent="login"><label for="username">账号</label><input id="username" name="username" v-model="username" type="text" autocomplete="username" placeholder="输入账号" /><label for="password">Web 密码</label><input id="password" name="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入访问密码" /><button class="primary-button" type="submit"><LogIn :size="17" />登录</button></form><p v-if="error" class="error-text" role="alert">{{ error }}</p></section>

    <template v-else>
      <p v-if="error" class="error-strip" role="alert"><X :size="16" />{{ error }}</p>
      <template v-if="!result && !loading">
        <WorkbenchView v-if="activeView === 'workbench'" :organization-plan-capability="organizationPlanCapability" :strm-full-capability="strmFullCapability" :strm-incremental-capability="strmIncrementalCapability" @navigate="navigateFromWorkbench" @search="searchFromWorkbench" />
        <HomeView v-if="activeView === 'home'" :catalog="homeCatalog" :loading="catalogLoading" :error="homeError" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" @navigate="selectView" @retry="retryHome" />
        <LibraryView v-else-if="activeView === 'movies' || activeView === 'tv'" :movies="catalogMovies" :loading="catalogLoading" :error="catalogError" :favorite-ids="favoriteIds" :genre-id="genreId" :year="year" :sort="sort" :media-type="activeView" :page="currentPage" :total-pages="totalPages" :total-results="totalResults" @open="openMovie" @favorite="toggleFavorite" @filters="loadDiscover" @page="loadPage" @retry="retryCatalog" />
        <CollectionView v-else-if="activeView === 'favorites' || activeView === 'history'" :mode="activeView" :movies="activeView === 'favorites' ? favorites : history" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" />
        <SettingsView v-else-if="activeView === 'settings'" :api="api" :initial-section="settingsInitialSection" @auto-start-enabled="inspectionAutoStartEnabled = $event" />
        <OrganizationWorkbenchView v-else-if="activeView === 'organization-plans' && organizationPlanEnabled" :api="api" :execution-supported="organizationExecutionSupported" />
        <OrganizationHistoryView v-else-if="activeView === 'organization-history' && organizationPlanEnabled" :api="api" />
        <LibraryWorkbenchView v-else-if="activeView === 'library'" :api="api" :organization-plan-capability="organizationPlanCapability" :strm-full-capability="strmFullCapability" :strm-incremental-capability="strmIncrementalCapability" :strm-cleanup-capability="strmCleanupCapability" :empty-directory-cleanup-capability="emptyDirectoryCleanupCapability" @open-settings="openOrganizationSettings" />
        <WorkflowCenterView v-else-if="activeView === 'workflows'" :api="api" @open-push-tasks="openTaskDrawer" />
        <NotificationCenterView v-else-if="activeView === 'notifications'" :api="api" @navigate="selectView" />
        <SearchView v-else-if="activeView === 'search' || activeView === 'popular'" v-model="searchInput" :loading="catalogLoading" :error="catalogError" :movies="catalogMovies" :heading="catalogHeading" :favorite-ids="favoriteIds" :page="currentPage" :total-pages="totalPages" :total-results="totalResults" @search="searchMovies" @reset="selectView('home')" @open="openMovie" @favorite="toggleFavorite" @page="loadPage" @retry="retryCatalog" />
      </template>
      <section v-else-if="loading && !result" class="detail-loading" aria-busy="true"><LoaderCircle class="spin" :size="24" /><strong>正在加载影视资料</strong><span>资源将在资料下方独立加载</span></section>
       <p v-if="result && !pushCapabilities.magnet && !pushCapabilities.share" class="warning-strip">115 推送当前不可用，推送按钮已禁用。</p>
       <p v-else-if="result && pushCapabilities.magnet && !pushCapabilities.share" class="warning-strip">磁力云下载可用，115 分享转存尚未验证</p>
       <section v-if="result" class="detail-workspace"><MovieView :result="result" :resources="resourceItems" :resource-facets="resourceFacets" :resource-total="resourceTotal" :resource-hidden-total="resourceHiddenTotal" :resource-page="resourcePage" :resource-page-size="resourcePageSize" :resource-total-pages="resourceTotalPages" :resource-kind="resourceKind" :resource-quality="resourceQuality" :resource-query="resourceQuery" :resource-sort="resourceSort" :resource-loading="resourceLoading" :resource-search-loading="resourceSearchLoading" :resource-error="resourceError" :source-names="searchSourceNames" :metadata-loading="metadataLoading" :metadata-error="metadataError" :metadata-stale="metadataStale" :pagination-unavailable="resourcePaginationUnavailable" :media-type="detailMediaType" :season-number="selectedSeason" :season-detail="seasonDetail" :season-detail-loading="seasonDetailLoading" :season-detail-error="seasonDetailError" :pushing-id="pushingId" :push-capabilities="pushCapabilities" :favorite="detailFavorite" :inspection-supported="inspectionSupported" :inspection-state="inspectionState" :inspection-completed="inspectionCompleted" :inspection-total="inspectionTotal" :inspection-failed="inspectionFailed" :inspection-error="inspectionError" :inspection-more-available="inspectionMoreAvailable" :inspection-retry-available="inspectionRetryAvailable" :inspection-started="inspectionSeenIds.size > 0" @push="startPush" @favorite="toggleFavorite(result.movie)" @refresh="refreshResources" @retry-metadata="loadMetadata(result.movie.tmdb_id, detailMediaType, result.movie.title === '正在加载影视资料' ? undefined : result.movie)" @season="selectSeason" @inspect-more="inspectMore" @retry-failed="retryFailed" @retry-page="resourcePaginationUnavailable ? () => loadResourcePage(currentResourceRoute(), 'replace') : refreshResources" @page="changeResourcePage" @kind="(value) => changeResourceFilter({ kind: value })" @quality="(value) => changeResourceFilter({ quality: value })" @query="changeResourceQuery" @sort="(value) => changeResourceFilter({ sort: value })" @page-size="(value) => changeResourceFilter({ pageSize: value })" @back="returnToBrowse" /></section>
    </template>
    <TaskDrawer :api="api" :tasks="tasks" :open="drawerOpen" @close="drawerOpen = false" @navigate="navigateFromTaskDrawer" @updated="updateTask" @loaded="replaceTasks" />
  </main>
</template>
