<script setup lang="ts">
import { LoaderCircle, LogIn, PanelRight, X } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "./api";
import SearchView from "./views/SearchView.vue";
import MovieView from "./views/MovieView.vue";
import TaskDrawer from "./components/TaskDrawer.vue";
import { extractMovieId, navigateHome, navigateToMovie } from "./router";
import type { MovieMetadata, ResourceSummary, SearchResponse, TaskResponse } from "./types";

const api = new ApiClient();
const password = ref("");
const query = ref("");
const authenticated = ref(false);
const loading = ref(false);
const catalogLoading = ref(false);
const error = ref("");
const result = ref<SearchResponse | null>(null);
const movies = ref<MovieMetadata[]>([]);
const catalogHeading = ref("当前热门");
const tasks = ref<TaskResponse[]>([]);
const pushingId = ref<string | null>(null);
const drawerOpen = ref(false);
const pushSupported = ref(true);
let pollTimer: number | undefined;

const hasActiveTasks = computed(() =>
  tasks.value.some((task) => task.state === "queued" || task.state === "submitting"),
);

async function loadCatalog(searchQuery = query.value.trim()) {
  catalogLoading.value = true;
  error.value = "";
  try {
    const response = searchQuery
      ? await api.searchMovies(searchQuery)
      : await api.popularMovies();
    movies.value = response.results;
    catalogHeading.value = searchQuery ? `“${searchQuery}”的搜索结果` : "当前热门";
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "电影目录加载失败，请稍后重试";
  } finally {
    catalogLoading.value = false;
  }
}

async function loadResources(id: number, refresh = false) {
  loading.value = true;
  error.value = "";
  try {
    result.value = await api.search(id, refresh);
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "资源搜索失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}

async function openMovie(movie: MovieMetadata) {
  navigateToMovie(movie.tmdb_id);
  await loadResources(movie.tmdb_id);
}

function resetCatalog() {
  query.value = "";
  void loadCatalog();
}

function returnHome() {
  navigateHome();
  result.value = null;
  error.value = "";
  if (!movies.value.length) void loadCatalog();
}

async function syncRoute() {
  const movieId = extractMovieId(window.location.pathname);
  if (movieId !== null) {
    await loadResources(movieId);
  } else {
    result.value = null;
    if (authenticated.value && !movies.value.length) await loadCatalog();
  }
}

async function initializeWorkspace() {
  const movieId = extractMovieId(window.location.pathname);
  if (movieId !== null) {
    await loadResources(movieId);
  } else {
    await loadCatalog();
  }
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

onMounted(async () => {
  try {
    const health = await api.health();
    if (typeof health.push_supported !== "boolean") throw new Error("invalid health response");
    pushSupported.value = health.push_supported;
    await api.me();
    authenticated.value = true;
    await initializeWorkspace();
  } catch {
    authenticated.value = false;
  }
  window.addEventListener("popstate", syncRoute);
});
onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
  window.removeEventListener("popstate", syncRoute);
});
</script>

<template>
  <main class="app-shell">
    <header class="topbar"><a class="brand" href="/">WATCH<span>/</span>ASSISTANT</a><div class="topbar-meta"><span class="status-dot" />LAN workspace <button v-if="authenticated" class="icon-button" title="推送记录" @click="drawerOpen = true"><PanelRight :size="18" /></button></div></header>
    <section v-if="!authenticated" class="auth-gate"><div class="auth-mark"><LogIn :size="20" /></div><p class="eyebrow">PRIVATE WORKSPACE</p><h1>进入观影工作台</h1><p>你的 PanSou 聚合和 115 推送只在本地网络可见。</p><form @submit.prevent="login"><label for="password">Web 密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入访问密码" /><button class="primary-button" type="submit"><LogIn :size="17" />登录</button></form><p v-if="error" class="error-text">{{ error }}</p></section>
    <template v-else>
      <p v-if="error" class="error-strip"><X :size="16" />{{ error }}</p>
      <SearchView v-if="!result && !loading" v-model="query" :loading="catalogLoading" :movies="movies" :heading="catalogHeading" @search="loadCatalog()" @reset="resetCatalog" @open="openMovie" />
      <section v-else-if="loading && !result" class="detail-loading"><LoaderCircle class="spin" :size="24" /><strong>正在聚合资源</strong><span>正在查询 PanSou 的磁力与 115 分享结果</span></section>
      <p v-if="!pushSupported && result" class="warning-strip">TgtoDrive 推送契约尚未验证，推送按钮已禁用。</p>
      <section v-if="result" class="detail-workspace"><MovieView :result="result" :pushing-id="pushingId" :push-supported="pushSupported" @push="push" @refresh="loadResources(result.movie.tmdb_id, true)" @back="returnHome" /></section>
    </template>
    <TaskDrawer :tasks="tasks" :open="drawerOpen" @close="drawerOpen = false" />
  </main>
</template>
