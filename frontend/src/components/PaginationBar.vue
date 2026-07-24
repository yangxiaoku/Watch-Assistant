<script setup lang="ts">
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "@lucide/vue";

defineProps<{ page: number; totalPages: number; totalResults: number }>();
defineEmits<{ page: [page: number] }>();
</script>

<template>
  <nav v-if="totalPages > 1" class="pagination" aria-label="分页">
    <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ totalResults }} 条</span>
    <div>
      <button type="button" :disabled="page <= 1" title="第一页" aria-label="第一页" @click="$emit('page', 1)"><ChevronsLeft :size="17" /></button>
      <button type="button" :disabled="page <= 1" title="上一页" aria-label="上一页" @click="$emit('page', page - 1)"><ChevronLeft :size="17" /></button>
      <strong>{{ page }}</strong>
      <button type="button" :disabled="page >= totalPages" title="下一页" aria-label="下一页" @click="$emit('page', page + 1)"><ChevronRight :size="17" /></button>
      <button type="button" :disabled="page >= totalPages" title="最后一页" aria-label="最后一页" @click="$emit('page', totalPages)"><ChevronsRight :size="17" /></button>
    </div>
  </nav>
</template>
