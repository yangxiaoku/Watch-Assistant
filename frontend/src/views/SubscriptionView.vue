<script setup lang="ts">
import {
  CircleCheck,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import { describeUiError } from "../errorCatalog";
import { formatTimestamp } from "../format";
import type {
  SubscriptionCreateRequest,
  SubscriptionResourceObservationResponse,
  SubscriptionResponse,
} from "../types";

const props = defineProps<{ api: ApiClient }>();

const items = ref<SubscriptionResponse[]>([]);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const createOpen = ref(false);
const createBusy = ref(false);
const createError = ref("");
const tmdbId = ref<string>("");
const mediaType = ref<"movie" | "tv">("movie");
const mode = ref<"remind" | "confirm" | "auto">("remind");
const selected = ref<SubscriptionResponse | null>(null);
const observations = ref<SubscriptionResourceObservationResponse[]>([]);
const observationsLoading = ref(false);
let mounted = false;

const STATUS_ZH: Record<string, string> = {
  active: "活跃",
  matched: "已匹配",
  paused: "已暂停",
  cancelled: "已取消",
  completed: "已完成",
  no_match: "无匹配",
};

const MODE_ZH: Record<string, string> = {
  remind: "提醒",
  confirm: "确认",
  auto: "自动",
};

const MEDIA_ZH: Record<string, string> = {
  movie: "电影",
  tv: "剧集",
};

const visibleItems = computed(() =>
  items.value.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)),
);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    items.value = await props.api.subscriptions();
  } catch (exception) {
    error.value = describeUiError(
      exception instanceof ApiError ? exception.code : "request_failed",
      422,
    ).message;
  } finally {
    loading.value = false;
  }
}

async function create() {
  const id = Number.parseInt(tmdbId.value, 10);
  if (!Number.isInteger(id) || id <= 0) {
    createError.value = "请输入有效的 TMDB 编号。";
    return;
  }
  createBusy.value = true;
  createError.value = "";
  try {
    const payload: SubscriptionCreateRequest = {
      tmdb_id: id,
      media_type: mediaType.value,
      mode: mode.value,
    };
    await props.api.createSubscription(payload);
    createOpen.value = false;
    tmdbId.value = "";
    await load();
  } catch (exception) {
    createError.value = describeUiError(
      exception instanceof ApiError ? exception.code : "request_failed",
      422,
    ).message;
  } finally {
    createBusy.value = false;
  }
}

async function check(item: SubscriptionResponse) {
  busy.value = true;
  error.value = "";
  try {
    await props.api.subscriptionCheck(item.id);
    await load();
  } catch (exception) {
    error.value = describeUiError(
      exception instanceof ApiError ? exception.code : "request_failed",
      422,
    ).message;
  } finally {
    busy.value = false;
  }
}

async function togglePause(item: SubscriptionResponse) {
  busy.value = true;
  error.value = "";
  try {
    if (item.status === "paused") {
      await props.api.subscriptionResume(item.id, item.revision);
    } else {
      await props.api.subscriptionPause(item.id, item.revision);
    }
    await load();
  } catch (exception) {
    error.value = describeUiError(
      exception instanceof ApiError ? exception.code : "request_failed",
      422,
    ).message;
  } finally {
    busy.value = false;
  }
}

async function showObservations(item: SubscriptionResponse) {
  selected.value = item;
  observationsLoading.value = true;
  observations.value = [];
  try {
    observations.value = await props.api.subscriptionObservations(item.id);
  } catch {
    observations.value = [];
  } finally {
    observationsLoading.value = false;
  }
}

onMounted(() => {
  mounted = true;
  void load();
});
onBeforeUnmount(() => {
  mounted = false;
});
</script>

<template>
  <section class="subscription-view" aria-label="订阅管理">
    <header class="view-header">
      <div>
        <h2>订阅</h2>
        <p class="view-subtitle">订阅关注的影视，自动检查新资源并推送通知。</p>
      </div>
      <button class="btn primary" type="button" @click="createOpen = !createOpen">
        <Plus :size="16" />新建订阅
      </button>
    </header>

    <p v-if="error" class="error-strip" role="alert">{{ error }}</p>

    <form v-if="createOpen" class="create-form" @submit.prevent="create">
      <label>
        TMDB 编号
        <input v-model="tmdbId" type="number" min="1" placeholder="例如 12345" />
      </label>
      <label>
        类型
        <select v-model="mediaType">
          <option value="movie">电影</option>
          <option value="tv">剧集</option>
        </select>
      </label>
      <label>
        模式
        <select v-model="mode">
          <option value="remind">提醒</option>
          <option value="confirm">确认</option>
          <option value="auto">自动</option>
        </select>
      </label>
      <div class="create-actions">
        <button class="btn primary" type="submit" :disabled="createBusy">
          <LoaderCircle v-if="createBusy" class="spin" :size="16" />创建
        </button>
        <button class="btn" type="button" @click="createOpen = false">取消</button>
      </div>
      <p v-if="createError" class="error-strip" role="alert">{{ createError }}</p>
    </form>

    <div v-if="loading" class="empty-state">
      <LoaderCircle class="spin" :size="24" />
    </div>
    <div v-else-if="items.length === 0" class="empty-state">
      <Search :size="24" />
      <p>还没有订阅。点击「新建订阅」关注一部影视。</p>
    </div>
    <div v-else class="subscription-list">
      <article
        v-for="item in visibleItems"
        :key="item.id"
        class="subscription-card"
      >
        <div class="subscription-main">
          <div class="subscription-title">
            <span class="badge">{{ MEDIA_ZH[item.media_type] }}</span>
            <strong>TMDB #{{ item.tmdb_id }}</strong>
            <span
              v-if="item.media_type === 'tv' && item.season_number != null"
              class="muted"
            >S{{ item.season_number }}</span>
          </div>
          <div class="subscription-meta">
            <span class="status-tag" :data-status="item.status">
              {{ STATUS_ZH[item.status] ?? item.status }}
            </span>
            <span class="muted">模式：{{ MODE_ZH[item.mode] ?? item.mode }}</span>
            <span class="muted">
              上次检查：{{ item.last_checked_at ? formatTimestamp(item.last_checked_at) : "从未" }}
            </span>
            <span v-if="item.last_match_count > 0" class="muted">
              最近匹配 {{ item.last_match_count }} 条
            </span>
            <span v-if="item.last_error_code" class="error-text">
              {{ describeUiError(item.last_error_code, 409).message }}
            </span>
          </div>
        </div>
        <div class="subscription-actions">
          <button
            class="btn small"
            type="button"
            :disabled="busy"
            title="立即检查"
            @click="check(item)"
          >
            <RefreshCw :size="14" />检查
          </button>
          <button
            class="btn small"
            type="button"
            :disabled="busy"
            title="查看已发现的资源"
            @click="showObservations(item)"
          >
            <Search :size="14" />资源
          </button>
          <button
            class="btn small"
            type="button"
            :disabled="busy"
            :title="item.status === 'paused' ? '恢复' : '暂停'"
            @click="togglePause(item)"
          >
            <Pause v-if="item.status !== 'paused'" :size="14" />
            <Play v-else :size="14" />
            {{ item.status === "paused" ? "恢复" : "暂停" }}
          </button>
          <button class="btn small ghost" type="button" :disabled="busy" title="取消订阅">
            <Trash2 :size="14" />取消
          </button>
        </div>
      </article>
    </div>

    <section v-if="selected" class="observations-panel" aria-label="订阅发现的资源">
      <header>
        <h3>TMDB #{{ selected.tmdb_id }} 已发现资源</h3>
        <button class="btn small ghost" type="button" @click="selected = null">关闭</button>
      </header>
      <div v-if="observationsLoading" class="empty-state">
        <LoaderCircle class="spin" :size="20" />
      </div>
      <div v-else-if="observations.length === 0" class="empty-state">
        <p>暂未发现资源。</p>
      </div>
      <ul v-else class="observation-list">
        <li v-for="obs in observations" :key="obs.resource_id">
          <CircleCheck :size="14" class="ok" />
          <span>{{ obs.name }}</span>
          <span class="muted">{{ obs.source }}</span>
          <span v-if="obs.size_bytes != null" class="muted">
            {{ (obs.size_bytes / 1024 / 1024 / 1024).toFixed(2) }} GB
          </span>
        </li>
      </ul>
    </section>
  </section>
</template>

<style scoped>
.subscription-view {
  padding: 1.25rem;
  max-width: 960px;
  margin: 0 auto;
}
.view-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1rem;
}
.view-subtitle {
  color: var(--muted, #8a8f98);
  font-size: 0.9rem;
  margin-top: 0.25rem;
}
.create-form {
  display: grid;
  gap: 0.75rem;
  padding: 1rem;
  border: 1px solid var(--border, #333a46);
  border-radius: 8px;
  margin-bottom: 1rem;
  background: var(--surface, #1c2128);
}
.create-form label {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: 0.85rem;
}
.create-form input,
.create-form select {
  padding: 0.5rem 0.6rem;
  border-radius: 6px;
  border: 1px solid var(--border, #333a46);
  background: var(--surface-2, #262b34);
  color: inherit;
}
.create-actions {
  display: flex;
  gap: 0.5rem;
}
.subscription-list {
  display: grid;
  gap: 0.75rem;
}
.subscription-card {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.9rem 1rem;
  border: 1px solid var(--border, #333a46);
  border-radius: 8px;
  background: var(--surface, #1c2128);
}
.subscription-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.subscription-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-top: 0.4rem;
  font-size: 0.85rem;
}
.subscription-actions {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  align-items: flex-end;
}
.badge {
  padding: 0.1rem 0.45rem;
  border-radius: 4px;
  background: var(--accent-soft, #2b4a6b);
  font-size: 0.75rem;
}
.status-tag {
  font-size: 0.8rem;
  font-weight: 600;
}
.status-tag[data-status="active"],
.status-tag[data-status="matched"] {
  color: #4ade80;
}
.status-tag[data-status="paused"] {
  color: #fbbf24;
}
.status-tag[data-status="cancelled"] {
  color: #f87171;
}
.muted {
  color: var(--muted, #8a8f98);
}
.error-text {
  color: #f87171;
}
.ok {
  color: #4ade80;
}
.observations-panel {
  margin-top: 1.25rem;
  padding: 1rem;
  border: 1px solid var(--border, #333a46);
  border-radius: 8px;
  background: var(--surface, #1c2128);
}
.observations-panel header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}
.observation-list {
  list-style: none;
  display: grid;
  gap: 0.4rem;
}
.observation-list li {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-size: 0.9rem;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2rem;
  color: var(--muted, #8a8f98);
}
.spin {
  animation: spin 0.9s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
