<script setup lang="ts">
import { CheckCircle2, Clock3, LoaderCircle, RefreshCw } from "@lucide/vue";
import { onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import type { OrganizationHistoryItem } from "../types";

const props = defineProps<{ api: ApiClient }>();
const items = ref<OrganizationHistoryItem[]>([]);
const nextCursor = ref<number | null>(null);
const loading = ref(false);
const error = ref("");

function formatTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

async function load(cursor?: number) {
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.organizationHistory(cursor, 20);
    items.value = cursor === undefined ? response.items : [...items.value, ...response.items];
    nextCursor.value = response.next_cursor;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "整理历史加载失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <main class="organization-history-page">
    <header class="organization-heading">
      <div>
        <p class="eyebrow">115 网盘</p>
        <h1>整理历史</h1>
        <p>这里记录已经完成归档的影片，数据来自已完成的整理操作。</p>
      </div>
      <button class="icon-button" type="button" title="刷新整理历史" aria-label="刷新整理历史" :disabled="loading" @click="load()"><RefreshCw :size="17" :class="{ spin: loading }" /></button>
    </header>

    <p v-if="error" class="error-strip">{{ error }}</p>
    <div v-if="loading && !items.length" class="organization-empty"><LoaderCircle class="spin" :size="22" /><span>正在加载整理历史</span></div>
    <div v-else-if="!items.length" class="organization-empty"><Clock3 :size="22" /><strong>暂无整理完成记录</strong><span>点击“开始整理”并完成影片归档后，记录会出现在这里。</span></div>
    <section v-else class="organization-history-list" aria-label="整理历史列表">
      <article v-for="item in items" :key="item.id" class="organization-history-item">
        <div class="organization-history-item-main">
          <CheckCircle2 :size="19" class="organization-history-success" />
          <div><h2>{{ item.title }}</h2><p>{{ item.source_name }}</p></div>
        </div>
        <dl class="organization-history-facts">
          <div><dt>归档位置</dt><dd>{{ item.target_path }}</dd></div>
          <div><dt>完成时间</dt><dd>{{ formatTime(item.completed_at) }}</dd></div>
          <div v-if="item.tmdb_id"><dt>TMDB</dt><dd>{{ item.tmdb_id }}</dd></div>
        </dl>
      </article>
      <button v-if="nextCursor !== null" class="secondary-button" type="button" :disabled="loading" @click="load(nextCursor!)">加载更多</button>
    </section>
  </main>
</template>
