<script setup lang="ts">
import { RefreshCw } from "@lucide/vue";
import ResourceTable from "../components/ResourceTable.vue";
import type { ResourceSummary, SearchResponse } from "../types";

defineProps<{ result: SearchResponse; pushingId: string | null }>();
defineEmits<{ push: [resource: ResourceSummary]; refresh: [] }>();
</script>

<template>
  <section class="movie-view">
    <header class="movie-header">
      <div><p class="eyebrow">TMDB {{ result.movie.tmdb_id }} · {{ result.movie.release_year ?? '年份未知' }}</p><h2>{{ result.movie.title }}</h2><p v-if="result.movie.original_title" class="original-title">{{ result.movie.original_title }}</p></div>
      <button class="secondary-button" @click="$emit('refresh')"><RefreshCw :size="16" />刷新</button>
    </header>
    <p v-if="result.movie.overview" class="movie-overview">{{ result.movie.overview }}</p>
    <div v-if="result.warnings.length" class="warning-strip">{{ result.warnings.join(' · ') }}</div>
    <ResourceTable :resources="result.results" :pushing-id="pushingId" @push="$emit('push', $event)" />
  </section>
</template>
