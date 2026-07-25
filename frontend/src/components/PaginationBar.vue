<script setup lang="ts">
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "@lucide/vue";
import { computed } from "vue";

const props = defineProps<{ page: number; totalPages: number; totalResults: number; loading?: boolean }>();
defineEmits<{ page: [page: number] }>();

const safeTotalPages = computed(() => Math.min(500, Math.max(1, Number.isInteger(props.totalPages) ? props.totalPages : 1)));
const safePage = computed(() => Math.min(safeTotalPages.value, Math.max(1, Number.isInteger(props.page) ? props.page : 1)));
</script>

<template>
  <nav v-if="safeTotalPages > 1" class="pagination" aria-label="分页" :aria-busy="loading ? 'true' : 'false'">
    <span>第 {{ safePage }} / {{ safeTotalPages }} 页 · 共 {{ totalResults }} 条<span v-if="loading" class="pagination-loading" role="status">正在加载</span></span>
    <div>
      <button type="button" :disabled="loading || safePage <= 1" title="第一页" aria-label="第一页" @click="$emit('page', 1)"><ChevronsLeft :size="17" /></button>
      <button type="button" :disabled="loading || safePage <= 1" title="上一页" aria-label="上一页" @click="$emit('page', safePage - 1)"><ChevronLeft :size="17" /></button>
      <strong>{{ safePage }}</strong>
      <button type="button" :disabled="loading || safePage >= safeTotalPages" title="下一页" aria-label="下一页" @click="$emit('page', safePage + 1)"><ChevronRight :size="17" /></button>
      <button type="button" :disabled="loading || safePage >= safeTotalPages" title="最后一页" aria-label="最后一页" @click="$emit('page', safeTotalPages)"><ChevronsRight :size="17" /></button>
    </div>
  </nav>
</template>
