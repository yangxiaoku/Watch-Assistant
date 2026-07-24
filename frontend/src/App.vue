<script setup lang="ts">
import { Clock3, Film, Flame, Heart, Home, LoaderCircle, LogIn, PanelRight, Search, X } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "./api";
import TaskDrawer from "./components/TaskDrawer.vue";
import {
  extractBrowseView,
  extractMovieId,
  navigateToMovie,
  navigateToSearch,
  navigateToView,
  type BrowseView,
} from "./router";
import type { HomeCatalogResponse, MovieMetadata, ResourceSummary, SearchResponse, TaskResponse } from "./types";
import CollectionView from "./views/CollectionView.vue";
import HomeView from "./views/HomeView.vue";
import LibraryView from "./views/LibraryView.vue";
import MovieView from "./views/MovieView.vue";
import SearchView from "./views/SearchView.vue";

const FAVORITES_KEY = "watch-assistant:favorites";
const HISTORY_KEY = "watch-assistant:history";
const api = new ApiClient();
const password = ref("");
const query = ref("");
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
const favorites = ref<MovieMetadata[]>(readStoredMovies(FAVORITES_KEY));
const history = ref<MovieMetadata[]>(readStoredMovies(HISTORY_KEY));
const tasks = ref<TaskResponse[]>([]);
const pushingId = ref<string | null>(null);
const drawerOpen = ref(false);
const pushSupported = ref(true);
let pollTimer: number | undefined;

const favoriteIds = computed(() => new Set(favorites.value.map((movie) => movie.tmdb_id)));
const detailFavorite = computed(() => result.value ? favoriteIds.value.has(result.value.movie.tmdb_id) : false);
const hasActiveTasks = computed(() => tasks.value.some((task) => task.state === "queued" || task.state === "submitting"));

const navItems = [
  { view: "home" as const, label: "首页", icon: Home },
  { view: "movies" as const, label: "电影", icon: Film },
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
  favorites.value = favoriteIds.value.has(movie.tmdb_id)
    ? favorites.value.filter((item) => item.tmdb_id !== movie.tmdb_id)
    : [movie, ...favorites.value].slice(0, 100);
  storeMovies(FAVORITES_KEY, favorites.value);
}

function recordHistory(movie: MovieMetadata) {
  history.value = [movie, ...history.value.filter((item) => item.tmdb_id !== movie.tmdb_id)].slice(0, 50);
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

async function loadDiscover(filters = { genreId: genreId.value, year: year.value, sort: sort.value }) {
  genreId.value = filters.genreId;
  year.value = filters.year;
  sort.value = filters.sort;
  catalogLoading.value = true;
  error.value = "";
  try {
    catalogMovies.value = (await api.discoverMovies(filters)).results;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "电影目录加载失败，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function loadPopular() {
  catalogLoading.value = true;
  error.value = "";
  try {
    catalogMovies.value = homeCatalog.value?.popular ?? (await api.popularMovies()).results;
    catalogHeading.value = "本周热门";
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "热门电影加载失败，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function performSearch(updateUrl: boolean) {
  const searchQuery = query.value.trim();
  if (!searchQuery) return;
  activeView.value = "search";
  previousView.value = "search";
  result.value = null;
  if (updateUrl) navigateToSearch(searchQuery);
  catalogLoading.value = true;
  error.value = "";
  catalogHeading.value = `“${searchQuery}”的搜索结果`;
  try {
    catalogMovies.value = (await api.searchMovies(searchQuery)).results;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "电影搜索失败，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function searchMovies() {
  await performSearch(true);
}

async function loadView(view: BrowseView) {
  if (view === "home") await loadHome();
  if (view === "movies") await loadDiscover();
  if (view === "popular") await loadPopular();
  if (view === "search") await performSearch(false);
}

async function selectView(view: Exclude<BrowseView, "search">) {
  result.value = null;
  error.value = "";
  activeView.value = view;
  previousView.value = view;
  navigateToView(view);
  await loadView(view);
}

async function loadResources(id: number, refresh = false) {
  loading.value = true;
  error.value = "";
  try {
    result.value = await api.search(id, refresh);
    recordHistory(result.value.movie);
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "资源搜索失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}

async function openMovie(movie: MovieMetadata) {
  previousView.value = activeView.value;
  recordHistory(movie);
  result.value = null;
  navigateToMovie(movie.tmdb_id);
  await loadResources(movie.tmdb_id);
}

async function returnToBrowse() {
  result.value = null;
  const view = previousView.value === "search" ? "search" : previousView.value;
  activeView.value = view;
  if (view === "search") {
    navigateToSearch(query.value.trim());
  } else {
    navigateToView(view);
  }
  await loadView(view);
}

async function initializeWorkspace() {
  const movieId = extractMovieId(window.location.pathname);
  if (movieId !== null) {
    await loadResources(movieId);
    return;
  }
  activeView.value = extractBrowseView(window.location.pathname);
  previousView.value = activeView.value;
  if (activeView.value === "search") query.value = new URLSearchParams(window.location.search).get("q") ?? "";
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
  if (!pushSupported.value) {
    error.value = "TgtoDrive 尚未提供稳定推送接口，当前已安全禁用真实推送。";
    return;
  }
  pushingId.value = resource.resource_id;
  error.value = "";
  try {
    const task = await api.createTask(resource.resource_id);
    tasks.value = [task, ...tasks.value.filter((item) => item.id !== task.id)].slice(0, 50);
    drawerOpen.value = true;
    ensurePolling();
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "推送失败";
  } finally {
    pushingId.value = null;
  }
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
  result.value = null;
  await initializeWorkspace();
}

onMounted(async () => {
  try {
    const health = await api.health();
    if (typeof health.push_supported !== "boolean") throw new Error("invalid health response");
    pushSupported.value = health.push_supported;
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
        <form class="top-search" role="search" @submit.prevent="searchMovies"><Search :size="17" /><input v-model="query" type="search" aria-label="搜索电影" placeholder="搜索电影名称" /><button type="submit" aria-label="提交搜索" title="搜索"><Search :size="17" /></button></form>
        <div class="topbar-actions">
          <button type="button" :class="{ active: activeView === 'favorites' && !result }" @click="selectView('favorites')"><Heart :size="17" />收藏</button>
          <button type="button" :class="{ active: activeView === 'history' && !result }" @click="selectView('history')"><Clock3 :size="17" />记录</button>
          <button class="icon-button" type="button" title="推送记录" aria-label="推送记录" @click="drawerOpen = true"><PanelRight :size="18" /></button>
        </div>
      </template>
      <div v-else class="topbar-meta"><span class="status-dot" />LAN workspace</div>
    </header>

    <section v-if="!authenticated" class="auth-gate"><div class="auth-mark"><LogIn :size="20" /></div><p class="eyebrow">PRIVATE WORKSPACE</p><h1>进入观影工作台</h1><p>你的 PanSou 聚合和 115 推送只在本地网络可见。</p><form @submit.prevent="login"><label for="password">Web 密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入访问密码" /><button class="primary-button" type="submit"><LogIn :size="17" />登录</button></form><p v-if="error" class="error-text">{{ error }}</p></section>

    <template v-else>
      <p v-if="error" class="error-strip"><X :size="16" />{{ error }}</p>
      <template v-if="!result && !loading">
        <HomeView v-if="activeView === 'home'" :catalog="homeCatalog" :loading="catalogLoading" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" @navigate="selectView" />
        <LibraryView v-else-if="activeView === 'movies'" :movies="catalogMovies" :loading="catalogLoading" :favorite-ids="favoriteIds" :genre-id="genreId" :year="year" :sort="sort" @open="openMovie" @favorite="toggleFavorite" @filters="loadDiscover" />
        <CollectionView v-else-if="activeView === 'favorites' || activeView === 'history'" :mode="activeView" :movies="activeView === 'favorites' ? favorites : history" :favorite-ids="favoriteIds" @open="openMovie" @favorite="toggleFavorite" />
        <SearchView v-else v-model="query" :loading="catalogLoading" :movies="catalogMovies" :heading="catalogHeading" :favorite-ids="favoriteIds" @search="searchMovies" @reset="selectView('home')" @open="openMovie" @favorite="toggleFavorite" />
      </template>
      <section v-else-if="loading && !result" class="detail-loading"><LoaderCircle class="spin" :size="24" /><strong>正在聚合资源</strong><span>正在查询 PanSou 的磁力与 115 分享结果</span></section>
      <p v-if="!pushSupported && result" class="warning-strip">TgtoDrive 推送契约尚未验证，推送按钮已禁用。</p>
      <section v-if="result" class="detail-workspace"><MovieView :result="result" :pushing-id="pushingId" :push-supported="pushSupported" :favorite="detailFavorite" @push="push" @favorite="toggleFavorite(result.movie)" @refresh="loadResources(result.movie.tmdb_id, true)" @back="returnToBrowse" /></section>
    </template>
    <TaskDrawer :tasks="tasks" :open="drawerOpen" @close="drawerOpen = false" />
  </main>
</template>
